import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import httpx
import os
from nostr_sdk import NostrWalletConnectUri, Nwc, MakeInvoiceRequest
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

OLLAMA_URL = "http://localhost:11434"
NWC_URI = os.environ["NWC_URI"]
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
INVOICE_EXPIRY = 3600  # seconds

# Max simultaneous Ollama inference calls — prevents GPU saturation
# when multiple IPs hit at the same time.
MAX_CONCURRENT_INFERENCE = 3

DB_PATH = Path(__file__).parent / "payments.db"

MODEL_PRICING: dict[str, int] = {
    "llama3:latest": 5,
    "qwen2.5:14b":  15,
}
DEFAULT_MODEL = "qwen2.5:14b"

nwc = Nwc(NostrWalletConnectUri.parse(NWC_URI))
limiter = Limiter(key_func=get_remote_address)

AVAILABLE_MODELS: dict[str, int] = {}
_inference_semaphore = asyncio.Semaphore(MAX_CONCURRENT_INFERENCE)


def _verify_preimage(preimage_hex: str, payment_hash_hex: str) -> bool:
    try:
        return hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest() == payment_hash_hex
    except Exception:
        return False


async def _init_db(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            payment_hash TEXT PRIMARY KEY,
            model        TEXT NOT NULL,
            created_at   REAL NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS spent_hashes (
            payment_hash TEXT PRIMARY KEY,
            spent_at     REAL NOT NULL
        )
    """)
    await db.commit()


async def _cleanup_loop(db: aiosqlite.Connection) -> None:
    while True:
        await asyncio.sleep(600)
        cutoff = time.time() - INVOICE_EXPIRY
        await db.execute("DELETE FROM pending_payments WHERE created_at < ?", (cutoff,))
        await db.commit()


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

    db = await aiosqlite.connect(DB_PATH)
    await _init_db(db)
    app.state.db = db
    app.state.limiter = limiter

    cleanup_task = asyncio.create_task(_cleanup_loop(db))
    yield
    cleanup_task.cancel()
    await db.close()


app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/info")
@limiter.limit("60/minute")
async def info(request: Request):
    return {
        "models": AVAILABLE_MODELS,
        "default_model": DEFAULT_MODEL,
        "protocol": "L402",
        "auth_format": "L402 <payment_hash>:<preimage>",
    }


@app.post("/complete")
@limiter.limit("20/minute")
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
    db: aiosqlite.Connection = request.app.state.db

    # --- Unauthenticated: issue invoice ---
    if not auth.startswith("L402 "):
        invoice_resp = await nwc.make_invoice(
            MakeInvoiceRequest(
                amount=price_sats * 1000,
                description=f"Ollama inference ({requested_model})",
                description_hash=None,
                expiry=INVOICE_EXPIRY,
            )
        )
        await db.execute(
            "INSERT OR REPLACE INTO pending_payments VALUES (?, ?, ?)",
            (invoice_resp.payment_hash, requested_model, time.time()),
        )
        await db.commit()
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

    # --- Authenticated: verify preimage ---
    parts = auth.removeprefix("L402 ").split(":")
    if len(parts) != 2:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid Authorization format. Expected: L402 <payment_hash>:<preimage>"},
        )
    payment_hash, preimage = parts

    async with db.execute(
        "SELECT 1 FROM spent_hashes WHERE payment_hash = ?", (payment_hash,)
    ) as cur:
        if await cur.fetchone():
            return JSONResponse(status_code=401, content={"error": "Payment already used"})

    async with db.execute(
        "SELECT model, created_at FROM pending_payments WHERE payment_hash = ?", (payment_hash,)
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        return JSONResponse(status_code=401, content={"error": "Unknown payment hash — request a new invoice"})

    paid_model, created_at = row

    if time.time() - created_at > INVOICE_EXPIRY:
        await db.execute("DELETE FROM pending_payments WHERE payment_hash = ?", (payment_hash,))
        await db.commit()
        return JSONResponse(status_code=402, content={"error": "Invoice expired — request a new one"})

    if paid_model != requested_model:
        return JSONResponse(
            status_code=401,
            content={"error": f"Model mismatch: paid for '{paid_model}', requesting '{requested_model}'"},
        )

    if not _verify_preimage(preimage, payment_hash):
        return JSONResponse(status_code=401, content={"error": "Invalid preimage"})

    await db.execute("INSERT INTO spent_hashes VALUES (?, ?)", (payment_hash, time.time()))
    await db.execute("DELETE FROM pending_payments WHERE payment_hash = ?", (payment_hash,))
    await db.commit()

    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return JSONResponse(status_code=400, content={"error": "messages array required"})

    # Global concurrency cap — queues requests rather than running all in parallel
    async with _inference_semaphore:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": paid_model, "messages": messages, "stream": stream},
            )

    return Response(content=resp.content, media_type="application/json")
