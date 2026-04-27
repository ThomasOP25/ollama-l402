# ollama-l402

L402 proxy for Ollama. Agents pay sats per inference request.

## Setup

```bash
cd ~/ollama-l402
source ~/ai-env/bin/activate
pip install fastapi uvicorn httpx nostr-sdk
```

## Run

```bash
export NWC_URI="nostrwalletconnect://..."
uvicorn proxy:app --host 0.0.0.0 --port 8000
```

## Expose publicly (for testing)

```bash
ngrok http 8000
```

## Endpoints

- `GET /info` — model, price, protocol
- `POST /complete` — L402-gated inference

## Request format

```json
{ "messages": [{ "role": "user", "content": "Hello" }] }
```
