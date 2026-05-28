#!/bin/bash
echo "Starting Kalshi scanner on port 8765..."
python kalshi_server.py &
SCANNER_PID=$!
sleep 6
echo "Starting Kalshi trader..."
python kalshi_trader.py
