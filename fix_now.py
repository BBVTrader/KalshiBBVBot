#!/usr/bin/env python3
import os, subprocess

os.chdir(os.path.expanduser("~/kalshi-platform"))

# ── Fix kalshi_trader.py ─────────────────────────────────────────────────────
trader = open("kalshi_trader.py").read()
lines = trader.splitlines()
trader_changed = False
for i, line in enumerate(lines):
    if "SCANNER_URL" in line and ("getenv" in line or "kalshi-trader" in line):
        print(f"FOUND trader line {i+1}: {line.strip()}")
        lines[i] = '    SCANNER_URL: str = "http://localhost:8765"'
        print(f"REPLACED with:          {lines[i].strip()}")
        trader_changed = True
        break
if trader_changed:
    open("kalshi_trader.py", "w").write("\n".join(lines))
else:
    print("ERROR: SCANNER_URL line not found in trader - showing all SCANNER_URL lines:")
    for i, l in enumerate(lines):
        if "SCANNER_URL" in l:
            print(f"  {i+1}: {l}")

# ── Fix kalshi_server.py ─────────────────────────────────────────────────────
server = open("kalshi_server.py").read()
lines2 = server.splitlines()
server_changed = False
for i, line in enumerate(lines2):
    if line.strip().startswith("PORT") and ("getenv" in line or "8765" in line) and "KALSHI" not in line:
        print(f"FOUND server line {i+1}: {line.strip()}")
        lines2[i] = "PORT = 8765"
        print(f"REPLACED with:         {lines2[i].strip()}")
        server_changed = True
        break
if server_changed:
    open("kalshi_server.py", "w").write("\n".join(lines2))
else:
    print("ERROR: PORT line not found in server - showing PORT lines:")
    for i, l in enumerate(lines2):
        if l.strip().startswith("PORT"):
            print(f"  {i+1}: {l}")

# ── Rewrite start.sh ─────────────────────────────────────────────────────────
open("start.sh", "w").write("""#!/bin/bash
echo "Starting Kalshi scanner on port 8765..."
python kalshi_server.py &
SCANNER_PID=$!
sleep 6
echo "Starting Kalshi trader..."
python kalshi_trader.py
""")
os.chmod("start.sh", 0o755)
print("REWROTE start.sh")

# ── Verify ───────────────────────────────────────────────────────────────────
print("\n=== VERIFICATION ===")
for f in ["kalshi_trader.py", "kalshi_server.py", "start.sh"]:
    result = subprocess.run(["grep", "-n", "SCANNER_URL\\|^PORT", f],
                           capture_output=True, text=True)
    if result.stdout:
        print(f"{f}:\n{result.stdout}")

# ── Git status before commit ─────────────────────────────────────────────────
print("=== GIT DIFF ===")
subprocess.run(["git", "diff", "--stat"])

# ── Commit ───────────────────────────────────────────────────────────────────
subprocess.run(["git", "add", "kalshi_server.py", "kalshi_trader.py", "start.sh"])
r = subprocess.run(["git", "commit", "-m",
    "fix: hardcode SCANNER_URL=localhost:8765 and PORT=8765 directly in source"],
    capture_output=True, text=True)
print(r.stdout or r.stderr)
r2 = subprocess.run(["git", "push"], capture_output=True, text=True)
print(r2.stdout or r2.stderr)
print("\nDone. Wait 90s then: curl -s https://kalshibbvbot.onrender.com/state | python3 -m json.tool | grep scanner_url")
