#!/bin/bash
export SCANNER_PORT=8765
echo "Starting Kalshi scanner on port 8765..."
python kalshi_server.py &
sleep 5
echo "Starting Kalshi trader on port $PORT..."
SCANNER_URL=http://localhost:8765 python kalshi_trader.py
