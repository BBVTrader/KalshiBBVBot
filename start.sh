#!/bin/bash
echo "Starting Kalshi scanner on internal port 8765..."
KALSHI_API_KEY="$KALSHI_API_KEY" \
KALSHI_API_SECRET="$KALSHI_API_SECRET" \
PORT=8765 python kalshi_server.py &
SCANNER_PID=$!

echo "Waiting for scanner to be ready..."
sleep 5

echo "Starting Kalshi trader on Render port $PORT..."
SCANNER_URL=http://localhost:8765 python kalshi_trader.py
