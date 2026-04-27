import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import httpx
import os
from nostr_sdk import NostrWalletConnectUri, Nwc, MakeInvoiceRequest

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


def _verify_preimage(preimage_hex: str, payment_hash_hex: str) -> bool:
    """Cryptographic proof of Lightning payment: sha256(preimage) == payment_hash."""
    try:
        preimage_bytes = bytes.fromhex(preimage_hex)
        return hashlib.sha256(preimage_bytes).hexdigest() == payment_hash_hex
    except Exception:
        return False


async def _cleanup_expired_pending():
    while True:
        await asyncio.sleep(600)
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


@app.get("/info")
async def info():
    return {
        "models": AVAILABLE_MODELS,
        "default_model": DEFAULT_MODEL,
        "protocol": "L402",
        "auth_format": "L402 <payment_hash>:<preimage>",
    }


@app.post("/complete")
async def complete(request: Request):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"error": "Request body too large"})

    stream = request.query_params.get("stream", "true").lower() != "false"
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
        _pending_payments[invoice_resp.payment_hash] = (requested_model, time.time())
        return JSONResponse(
            status_code=402,
            content={
                "invoice": invoice_resp.invoice,
                "payment_hash": invoice_resp.payment_hash,
                "model": requested_model,
                "price_sats": price_sats,
            },
            headers={"WWW-Authenticate": f'L402 invoice="{invoice_resp.invoice}"'},
        )

    # Parse L402 <payment_hash>:<preimage>
    parts = auth.removeprefix("L402 ").split(":")
    if len(parts) != 2:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid Authorization format. Expected: L402 <payment_hash>:<preimage>"},
        )
    payment_hash, preimage = parts

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

    # Cryptographic proof: sha256(preimage) == payment_hash. No NWC round-trip needed.
    if not _verify_preimage(preimage, payment_hash):
        return JSONResponse(status_code=401, content={"error": "Invalid preimage"})

    _spent_hashes.add(payment_hash)
    del _pending_payments[payment_hash]

    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return JSONResponse(status_code=400, content={"error": "messages array required"})

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": paid_model, "messages": messages, "stream": stream},
        )

    return Response(content=resp.content, media_type="application/json")
