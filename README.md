# ollama-l402

A pay-per-request AI API using Lightning Network micropayments (L402 protocol). Each request requires a Lightning payment — no accounts, no API keys.

## How it works

1. Send a request → receive a Lightning invoice
2. Pay the invoice in any Lightning wallet
3. Resend the request with the `payment_hash` → get your AI response

## Live API

**Base URL:** `https://afraid-celtic-copy.ngrok-free.dev`

### Check available models

```bash
curl https://afraid-celtic-copy.ngrok-free.dev/info
```

```json
{
  "models": { "llama3:latest": 5, "qwen2.5:14b": 15 },
  "default_model": "qwen2.5:14b",
  "protocol": "L402"
}
```

### Step 1 — Get an invoice

```bash
curl -X POST https://afraid-celtic-copy.ngrok-free.dev/complete \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Your question here"}]}'
```

Response:
```json
{
  "invoice": "lnbc...",
  "payment_hash": "abc123...",
  "model": "qwen2.5:14b",
  "price_sats": 15
}
```

### Step 2 — Pay the invoice

Paste the `invoice` string into any Lightning wallet: Phoenix, Breez, Zeus, Wallet of Satoshi, Muun.

### Step 3 — Send your authenticated request

```bash
curl -X POST https://afraid-celtic-copy.ngrok-free.dev/complete \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <payment_hash>" \
  -d '{"messages": [{"role": "user", "content": "Your question here"}]}'
```

Add `"model": "llama3:latest"` to the body to use the cheaper model.

## Models

| Model | Price | Description |
|---|---|---|
| `qwen2.5:14b` | 15 sats | Default — better reasoning |
| `llama3:latest` | 5 sats | Faster, good for simple questions |

## Notes

- Responses arrive in ~2 seconds
- Each payment hash is single-use
- Invoices expire after 1 hour
- Responses stream as NDJSON — each line is a JSON chunk, final line has `"done": true`

## Feedback

Open an issue on this repo with any bugs, suggestions, or questions.

## Self-hosting

### Requirements

- Python 3.12+
- [Ollama](https://ollama.com) running locally with at least one model pulled
- A [Nostr Wallet Connect](https://nwc.dev) compatible Lightning wallet

### Setup

```bash
git clone https://github.com/ThomasOP25/ollama-l402
cd ollama-l402
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
export NWC_URI="nostr+walletconnect://..."
uvicorn proxy:app --host 0.0.0.0 --port 8000
```
