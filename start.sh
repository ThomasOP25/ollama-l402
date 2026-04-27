#!/usr/bin/env bash
set -euo pipefail

OLLAMA_DIR="$HOME/ollama-l402"
PROXY_DIR="$HOME/402-proxy"
NWC_FILE="$HOME/l402_llm_api_nwc.txt"
ENV_FILE="$HOME/.config/402-proxy.env"
DOMAIN="afraid-celtic-copy.ngrok-free.dev"

# --- Load secrets ---
export NWC_URI=$(cat "$NWC_FILE")
export NWC_URL="$NWC_URI"
export MACAROON_SECRET=$(grep ^MACAROON_SECRET "$ENV_FILE" | cut -d= -f2)

# --- Stop existing processes ---
echo "Stopping existing processes..."
pkill -f "uvicorn proxy:app" 2>/dev/null || true
pkill -f "tsx src/index.ts"  2>/dev/null || true
pkill -f "ngrok http"        2>/dev/null || true
sleep 2

# --- Start ollama-l402 (port 8000) ---
echo "Starting ollama-l402..."
cd "$OLLAMA_DIR"
nohup "$HOME/ai-env/bin/uvicorn" proxy:app --host 0.0.0.0 --port 8000 \
  > /tmp/ollama-l402.log 2>&1 &
OLLAMA_PID=$!

# --- Start 402-proxy (port 3000) ---
echo "Starting 402-proxy..."
cd "$PROXY_DIR"
nohup yarn dev > /tmp/402-proxy.log 2>&1 &
PROXY_PID=$!

# --- Start ngrok ---
echo "Starting ngrok..."
nohup ngrok http --domain="$DOMAIN" 8000 > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!

# --- Wait and verify ---
echo "Waiting for services..."
sleep 6

INFO=$(curl -sf "https://$DOMAIN/info" 2>/dev/null) && {
  echo ""
  echo "=== All services up ==="
  echo "$INFO" | python3 -m json.tool
  echo ""
  echo "URL: https://$DOMAIN"
} || {
  echo ""
  echo "ERROR: API did not respond. Check logs:"
  echo "  tail -20 /tmp/ollama-l402.log"
  echo "  tail -20 /tmp/402-proxy.log"
  echo "  tail -20 /tmp/ngrok.log"
  exit 1
}

echo "PIDs  ollama=$OLLAMA_PID  proxy=$PROXY_PID  ngrok=$NGROK_PID"
echo "Logs  /tmp/ollama-l402.log  /tmp/402-proxy.log  /tmp/ngrok.log"
