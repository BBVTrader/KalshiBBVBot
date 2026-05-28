#!/bin/bash
export SCANNER_PORT=8765
export SCANNER_URL=http://localhost:8765
python kalshi_server.py &
sleep 6
python kalshi_trader.py
