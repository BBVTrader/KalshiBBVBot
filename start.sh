#!/bin/bash
echo "Starting Kalshi scanner (port 8080)..."
PORT=8080 python kalshi_server.py &
SCANNER_PID=$!

sleep 4

echo "Starting Kalshi trader (port 10000)..."
SCANNER_URL=http://localhost:8080 PORT=10000 python kalshi_trader.py
