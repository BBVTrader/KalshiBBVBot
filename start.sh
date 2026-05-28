#!/bin/bash
echo "Starting Kalshi scanner on port 8181..."
KALSHI_API_KEY="$KALSHI_API_KEY" KALSHI_API_SECRET="$KALSHI_API_SECRET" SCANNER_PORT=8181 python kalshi_server.py &
SCANNER_PID=$!
sleep 8
echo "Starting Kalshi trader..."
SCANNER_URL=http://localhost:8181 python kalshi_trader.py
