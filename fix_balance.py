import re

path = '/Users/robertbiddlev/kalshi-platform/kalshi_trader.py'
code = open(path).read()

# 1. Add a function to fetch real Kalshi balance after _rsa_sign function
fetch_balance_func = '''

def fetch_kalshi_balance() -> float:
    """Fetch real account balance from Kalshi API."""
    try:
        path = "/trade-api/v2/portfolio/balance"
        headers = _rsa_sign("GET", path)
        req = urllib.request.Request(CFG.KALSHI_BASE + "/portfolio/balance", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        # balance is in cents
        bal = data.get("balance", 0)
        return float(bal) / 100.0
    except Exception as e:
        log.debug("Balance fetch failed: %s", e)
        return CFG.TOTAL_CAPITAL

'''

# Insert after the _public_headers function
insert_after = "def _public_headers() -> dict:\n    \"\"\"Headers for unauthenticated public Kalshi reads (markets endpoint).\"\"\"\n    return {\"Content-Type\": \"application/json\", \"Accept\": \"application/json\"}"

if insert_after in code:
    code = code.replace(insert_after, insert_after + fetch_balance_func)
    print("Added fetch_kalshi_balance function")
else:
    print("ERROR: insert point not found")
    exit(1)

# 2. Update the run() function to fetch real balance at start
old_capital = "    capital = CFG.TOTAL_CAPITAL"
new_capital = """    # Fetch real Kalshi balance
    real_balance = fetch_kalshi_balance()
    if real_balance > 0:
        capital = real_balance
        log.info("Real Kalshi balance: $%.2f", capital)
    else:
        capital = CFG.TOTAL_CAPITAL
        log.info("Using config capital: $%.2f", capital)"""

if old_capital in code:
    code = code.replace(old_capital, new_capital)
    print("Updated capital initialization")
else:
    print("ERROR: capital init not found")
    exit(1)

# 3. Update /state endpoint to show real balance
old_state = '"total_capital":    CFG.TOTAL_CAPITAL,'
new_state = '"total_capital":    capital if capital != CFG.TOTAL_CAPITAL else fetch_kalshi_balance(),'

# Actually update the state response to use a global capital var
# First make capital a global
old_shared = '_shared = {"risk": None, "cycle": 0, "started_at": time.time()}'
new_shared = '_shared = {"risk": None, "cycle": 0, "started_at": time.time(), "capital": 0.0}'

if old_shared in code:
    code = code.replace(old_shared, new_shared)
    print("Updated _shared dict")

# Update capital assignment in run() to also store in _shared
old_log = '    log.info("Real Kalshi balance: $%.2f", capital)'
new_log = '    log.info("Real Kalshi balance: $%.2f", capital)\n    try:\n        _shared["capital"] = capital\n    except Exception:\n        pass'
code = code.replace(old_log, new_log)

# Update /state to return real capital
old_total = '"total_capital":    CFG.TOTAL_CAPITAL,'
new_total = '"total_capital":    _shared.get("capital", CFG.TOTAL_CAPITAL),'
code = code.replace(old_total, new_total)
print("Updated /state total_capital")

import ast
try:
    ast.parse(code)
    open(path, 'w').write(code)
    print("SUCCESS: syntax OK, file written")
except SyntaxError as e:
    print("SYNTAX ERROR:", e)
