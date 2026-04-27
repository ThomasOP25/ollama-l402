import asyncio
import base64
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import httpx
import os
from nostr_sdk import NostrWalletConnectUri, Nwc, MakeInvoiceRequest, LookupInvoiceRequest

OLLAMA_URL = "http://localhost:11434"
NWC_URI = os.environ["NWC_URI"]
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
INVOICE_EXPIRY = 3600  # seconds — must match MakeInvoiceRequest expiry

MODEL_PRICING: dict[str, int] = {
    "llama3:latest": 5,
    "qwen2.5:14b":  15,
}
DEFAULT_MODEL = "qwen2.5:14b"

nwc = Nwc(NostrWalletConnectUri.parse(NWC_URI))

AVAILABLE_MODELS: dict[str, int] = {}

# payment_hash -> (model, created_at): cleared after use or expiry.
_pending_payments: dict[str, tuple[str, float]] = {}
_spent_hashes: set[str] = set()


def _hash_to_token(payment_hash_hex: str) -> str:
    """Encode a payment_hash hex string as a base64 token for L402 headers."""
    return base64.b64encode(bytes.fromhex(payment_hash_hex)).decode()


def _token_to_hash(token: str) -> str:
    """Decode a base64 L402 token back to a payment_hash hex string."""
    return base64.b64decode(token).hex()


async def _cleanup_expired_pending():
    """Periodically remove pending payment entries older than invoice expiry."""
    while True:
        await asyncio.sleep(600)  # run every 10 minutes
        cutoff = time.time() - INVOICE_EXPIRY
        expired = [h for h, (_, ts) in _pending_payments.items() if ts < cutoff]
        for h in expired:
            del _pending_payments[h]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global AVAILABLE_MODELS
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            installed = {m["name"] for m in resp.json()["models"]}
        AVAILABLE_MODELS = {m: p for m, p in MODEL_PRICING.items() if m in installed}
    except Exception:
        AVAILABLE_MODELS = dict(MODEL_PRICING)

    cleanup_task = asyncio.create_task(_cleanup_expired_pending())
    yield
    cleanup_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/models")
async def models():
    """Public endpoint: list available models and pricing. No payment required."""
    return {
        "models": [{"name": m, "price_sats": p} for m, p in AVAILABLE_MODELS.items()],
        "default_model": DEFAULT_MODEL,
    }


@app.get("/info")
async def info():
    return {
        "models": AVAILABLE_MODELS,
        "default_model": DEFAULT_MODEL,
        "protocol": "L402",
    }


@app.post("/complete")
async def complete(request: Request):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"error": "Request body too large"})

    body = await request.json()
    requested_model = body.get("model", DEFAULT_MODEL)

    if requested_model not in AVAILABLE_MODELS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Unknown model. Available: {list(AVAILABLE_MODELS.keys())}"},
        )

    price_sats = AVAILABLE_MODELS[requested_model]
    auth = request.headers.get("Authorization", "")

    if not auth.startswith("L402 "):
        invoice_resp = await nwc.make_invoice(
            MakeInvoiceRequest(
                amount=price_sats * 1000,
                description=f"Ollama inference ({requested_model})",
                description_hash=None,
                expiry=INVOICE_EXPIRY,
            )
        )
        token = _hash_to_token(invoice_resp.payment_hash)
        return JSONResponse(
            status_code=402,
            content={
                "invoice": invoice_resp.invoice,
                "payment_hash": invoice_resp.payment_hash,
                "model": requested_model,
                "price_sats": price_sats,
            },
            headers={
                "WWW-Authenticate": f'L402 token="{token}", invoice="{invoice_resp.invoice}"',
            },
        )

    # Parse Authorization: L402 <token>:<preimage>  or legacy  L402 <payment_hash>
    credential = auth.removeprefix("L402 ").strip()

    if ":" in credential:
        # Spec-compliant: L402 <token>:<preimage>
        token_part, preimage_part = credential.split(":", 1)
        try:
            payment_hash = _token_to_hash(token_part)
        except Exception:
            return JSONResponse(status_code=401, content={"error": "Invalid token encoding"})
    else:
        # Legacy fallback: L402 <payment_hash_hex>
        payment_hash = credential

    if payment_hash in _spent_hashes:
        return JSONResponse(status_code=401, content={"error": "Payment already used"})

    pending = _pending_payments.get(payment_hash)
    if pending is None:
        return JSONResponse(status_code=401, content={"error": "Unknown payment hash — request a new invoice"})

    paid_model, created_at = pending

    if time.time() - created_at > INVOICE_EXPIRY:
        del _pending_payments[payment_hash]
        return JSONResponse(status_code=402, content={"error": "Invoice expired — request a new one"})

    if paid_model != requested_model:
        return JSONResponse(
            status_code=401,
            content={"error": f"Model mismatch: paid for '{paid_model}', requesting '{requested_model}'"},
        )

    try:
        lookup = await nwc.lookup_invoice(
            LookupInvoiceRequest(payment_hash=payment_hash, invoice=None)
        )
        if not lookup.settled_at:
            return JSONResponse(status_code=402, content={"error": "Invoice not paid"})

        # L402 spec: verify preimage proof if provided via token:preimage format.
        # The nostr_sdk LookupInvoiceResponse exposes a `preimage` field after
        # settlement in most NWC implementations. If available, we verify it
        # matches what the client supplied — this proves the client actually
        # paid (preimage is only revealed after settlement).
        #
        # TODO: If nostr_sdk doesn't reliably expose lookup.preimage for all
        # NWC providers, the preimage check silently degrades to a trust-on-
        # payment_hash model. This is a known deviation from the L402 spec.
        # The payment_hash approach is still replay-resistant due to the
        # _spent_hashes set, but a preimage is cryptographically stronger
        # proof of payment.
        if ":" in credential:
            lookup_preimage = getattr(lookup, "preimage", None)
            if lookup_preimage and lookup_preimage != preimage_part:
                return JSONResponse(status_code=401, content={"error": "Preimage mismatch"})
    except Exception:
        return JSONResponse(status_code=401, content={"error": "Payment verification failed"})

    _spent_hashes.add(payment_hash)
    del _pending_payments[payment_hash]

    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return JSONResponse(status_code=400, content={"error": "messages array required"})

    # Determine streaming mode: query param overrides body default
    stream = body.get("stream", True)
    if request.query_params.get("stream") == "false":
        stream = False

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": paid_model, "messages": messages},
        )

    if not stream:
        # Collect all Ollama NDJSON chunks into a single response
        full_content = []
        model_name = paid_model
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json as _json
                chunk = _json.loads(line)
                if chunk.get("message", {}).get("content"):
                    full_content.append(chunk["message"]["content"])
                if chunk.get("model"):
                    model_name = chunk["model"]
            except Exception:
                pass
        return JSONResponse(content={
            "model": model_name,
            "content": "".join(full_content),
            "done": True,
        })

    return Response(content=resp.content, media_type="application/json")
