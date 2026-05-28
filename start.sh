#!/bin/bash
echo "Starting Kalshi scanner on port 8765..."
SCANNER_PORT=8765 python kalshi_server.py &
SCANNER_PID=$!
sleep 5

echo "Starting Kalshi trader on Render port $PORT..."
SCANNER_URL=http://localhost:8765 python kalshi_trader.py
