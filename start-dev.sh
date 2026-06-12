#!/bin/bash
# Start backend + ngrok tunnel in one command.
# Usage: ./start-dev.sh

set -e

BACKEND_DIR="$(cd "$(dirname "$0")/leadcaller/backend" && pwd)"
NGROK_DOMAIN="dweller-tinkling-mutiny.ngrok-free.dev"

# ── Cleanup on exit ──────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "Shutting down..."
  kill "$BACKEND_PID" "$NGROK_PID" 2>/dev/null
  wait "$BACKEND_PID" "$NGROK_PID" 2>/dev/null
  echo "Done."
}
trap cleanup EXIT INT TERM

# ── Backend ───────────────────────────────────────────────────────────────────
echo "Starting backend..."
cd "$BACKEND_DIR"
source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Wait until backend is up
for i in $(seq 1 15); do
  sleep 1
  curl -s http://localhost:8000/health > /dev/null 2>&1 && break
  if [ "$i" -eq 15 ]; then
    echo "ERROR: Backend failed to start. Check logs above."
    exit 1
  fi
done
echo "✓ Backend running on http://localhost:8000"

# ── ngrok ─────────────────────────────────────────────────────────────────────
echo "Starting ngrok tunnel..."
ngrok http --domain="$NGROK_DOMAIN" 8000 --log=stdout --log-level=warn &
NGROK_PID=$!
sleep 2

echo "✓ Tunnel running at https://$NGROK_DOMAIN"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Dashboard → https://leadcaller-dashboard.vercel.app"
echo "  Backend   → https://$NGROK_DOMAIN"
echo "  Local     → http://localhost:8000"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Press Ctrl+C to stop everything."
echo ""

# Keep running, tail backend logs
wait "$BACKEND_PID"
