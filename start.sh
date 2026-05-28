#!/bin/bash
while true; do
    echo "Starting Kalshi scanner on port 8765..."
    KALSHI_API_KEY="$KALSHI_API_KEY" KALSHI_API_SECRET="$KALSHI_API_SECRET" SCANNER_PORT=8765 python kalshi_server.py &
    SCANNER_PID=$!
    sleep 6
    echo "Starting Kalshi trader on port $PORT..."
    python kalshi_trader.py
    echo "Trader exited, restarting in 5s..."
    kill $SCANNER_PID 2>/dev/null
    sleep 5
done
