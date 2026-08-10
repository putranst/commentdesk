#!/bin/bash
# Launch Comment Desk with public tunnel
# Prerequisites: bore (brew install bore-cli), python3

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 Starting Comment Desk..."
cd "$DIR"

# Kill old instances
lsof -ti :8765 | xargs kill -9 2>/dev/null || true

# Start server
python3 app.py &
SERVER_PID=$!
sleep 3

# Start bore tunnel
echo "🌐 Creating public tunnel..."
bore local 8765 --to bore.pub 2>&1 | while read line; do
    echo "$line"
    if echo "$line" | grep -q "listening at"; then
        PORT=$(echo "$line" | grep -oP 'bore\.pub:\K\d+')
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Main App:    http://bore.pub:$PORT"
        echo "  Admin:       http://bore.pub:$PORT/admin"
        echo "  Password:    suluh2026"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    fi
done &
BORE_PID=$!

# Trap cleanup
trap "kill $SERVER_PID $BORE_PID 2>/dev/null; exit" INT TERM

echo "✅ Server running on http://localhost:8765"
echo "📊 Admin dashboard: http://localhost:8765/admin"
echo "🔑 Password: suluh2026"
echo ""
echo "Press Ctrl+C to stop"

wait $SERVER_PID
