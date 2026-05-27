path = '/Users/robertbiddlev/kalshi-platform/kalshi_trader.py'
code = open(path).read()

# 1. Add order latency logging in place_order function
old = '''    req = urllib.request.Request(
        CFG.KALSHI_BASE + "/portfolio/orders", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())'''

new = '''    req = urllib.request.Request(
        CFG.KALSHI_BASE + "/portfolio/orders", data=body, headers=headers, method="POST")
    try:
        _t0 = time.time()
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        _latency = (time.time() - _t0) * 1000
        log.info("Order latency: %.0fms  ticker=%s", _latency, ticker)
        return result'''

if old in code:
    code = code.replace(old, new)
    print("1. Added order latency logging")
else:
    print("1. ERROR: place_order pattern not found")

# 2. Add cycle timing at start and end of main loop
old2 = '        log.info("-- Cycle %d  %s ------------------------------",'
new2 = '        _cycle_start = time.time()\n        log.info("-- Cycle %d  %s ------------------------------",'
if old2 in code:
    code = code.replace(old2, new2)
    print("2. Added cycle start timer")
else:
    print("2. ERROR: cycle start not found")

old3 = '        time.sleep(CFG.SCAN_INTERVAL)'
new3 = '        _cycle_ms = (time.time() - _cycle_start) * 1000\n        log.info("Cycle complete in %.0fms | sleeping %ds", _cycle_ms, CFG.SCAN_INTERVAL)\n        time.sleep(CFG.SCAN_INTERVAL)'
# Only replace the last occurrence (in the main loop)
last_idx = code.rfind(old3)
if last_idx >= 0:
    code = code[:last_idx] + new3 + code[last_idx+len(old3):]
    print("3. Added cycle elapsed logging")
else:
    print("3. ERROR: sleep not found")

import ast
try:
    ast.parse(code)
    open(path, 'w').write(code)
    print("SUCCESS: syntax OK")
except SyntaxError as e:
    print("SYNTAX ERROR:", e)
