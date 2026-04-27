# ollama-l402

A pay-per-request AI API using Lightning Network micropayments (L402 protocol). Each request requires a Lightning payment — no accounts, no API keys.

## How it works

1. Send a request → receive a Lightning invoice
2. Pay the invoice in any Lightning wallet
3. Resend the request with the `Authorization` header → get your AI response

## Live API

**Base URL:** `https://afraid-celtic-copy.ngrok-free.dev`

### Check available models

```bash
curl https://afraid-celtic-copy.ngrok-free.dev/models
```

```json
{
  "models": [
    {"name": "llama3:latest", "price_sats": 5},
    {"name": "qwen2.5:14b", "price_sats": 15}
  ],
  "default_model": "qwen2.5:14b"
}
```

### Step 1 — Get an invoice

```bash
curl -X POST https://afraid-celtic-copy.ngrok-free.dev/complete \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Your question here"}]}'
```

Response (HTTP 402 with L402 challenge header):
```json
{
  "invoice": "lnbc...",
  "payment_hash": "abc123...",
  "model": "qwen2.5:14b",
  "price_sats": 15
}
```

The `WWW-Authenticate` header contains the L402 challenge:
```
WWW-Authenticate: L402 token="<base64>", invoice="<bolt11>"
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

**Spec-compliant format** (preferred):
```bash
curl -X POST https://afraid-celtic-copy.ngrok-free.dev/complete \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <base64_token>:<preimage>" \
  -d '{"messages": [{"role": "user", "content": "Your question here"}]}'
```

Add `"model": "llama3:latest"` to the body to use the cheaper model.

### Non-streaming responses

Add `?stream=false` to get a single JSON response instead of NDJSON chunks:

```bash
curl -X POST "https://afraid-celtic-copy.ngrok-free.dev/complete?stream=false" \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <payment_hash>" \
  -d '{"messages": [{"role": "user", "content": "Your question here"}]}'
```

Response:
```json
{"model": "qwen2.5:14b", "content": "full response text", "done": true}
```

## Models

| Model | Price | Description |
|---|---|---|
| `qwen2.5:14b` | 15 sats | Default — better reasoning |
| `llama3:latest` | 5 sats | Faster, good for simple questions |

## For AI Agents

Autonomous agents can interact with this API using [Nostr Wallet Connect (NWC)](https://nwc.dev) for fully automated Lightning payments. No human interaction required.

### Automated L402 flow

```
1. GET /models          → Check available models and prices
2. POST /complete       → Get a 402 with Lightning invoice
3. Pay via NWC          → Use your NWC wallet to settle the invoice
4. POST /complete       → Resend with Authorization header, get response
```

### Step-by-step example

**1. Discover models and pricing**
```bash
curl -s https://afraid-celtic-copy.ngrok-free.dev/models
```

**2. Request inference (triggers 402 with invoice)**
```bash
curl -s -X POST https://afraid-celtic-copy.ngrok-free.dev/complete \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Explain L402 in one sentence"}]}'
```

Extract the `invoice` and `payment_hash` from the JSON response.

**3. Pay the invoice via NWC**

Using a shell script with [Alby](https://getalby.com) NWC:
```bash
# Example: pay using coinos-nwc.sh or any NWC-compatible tool
./coinos-nwc.sh pay lnbc150n1p...
```

Or programmatically with `nostr_sdk` in Python:
```python
from nostr_sdk import NostrWalletConnectUri, Nwc, PayInvoiceRequest

nwc = Nwc(NostrWalletConnectUri.parse(NWC_URI))
result = await nwc.pay_invoice(PayInvoiceRequest(invoice="lnbc150n1p..."))
print(f"Paid! Preimage: {result.preimage}")
```

**4. Send the authenticated request**

Legacy format (backward compatible):
```bash
curl -s -X POST https://afraid-celtic-copy.ngrok-free.dev/complete \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <payment_hash>" \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Explain L402 in one sentence"}]}'
```

Spec-compliant format (recommended):
```bash
# The token is base64(payment_hash_hex), preimage comes from pay_invoice result
curl -s -X POST https://afraid-celtic-copy.ngrok-free.dev/complete \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <token>:<preimage>" \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Explain L402 in one sentence"}]}'
```

**5. For non-streaming responses** (easier to parse):
```bash
curl -s -X POST "https://afraid-celtic-copy.ngrok-free.dev/complete?stream=false" \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <payment_hash>" \
  -d '{"model":"qwen2.5:14b","messages":[{"role":"user","content":"Explain L402 in one sentence"}]}'
# → {"model":"qwen2.5:14b","content":"...","done":true}
```

### Budget considerations

- Check `/models` before each request to verify current pricing
- Prices are in satoshis (1 sat = 0.00000001 BTC ≈ $0.001 USD)
- Typical cost: 5–15 sats per request

## Notes

- Responses arrive in ~2 seconds
- Each payment hash is single-use
- Invoices expire after 1 hour
- Responses stream as NDJSON by default — each line is a JSON chunk, final line has `"done": true`
- Add `?stream=false` for a single JSON response

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
