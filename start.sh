#!/bin/bash
# Start the Kalshi server and trader together
echo "Starting Kalshi server..."
python kalshi_server.py &

# Wait a few seconds so the server is ready before the trader starts
sleep 3

echo "Starting Kalshi trader..."
python kalshi_trader.py
