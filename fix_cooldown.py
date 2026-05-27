import ast

path = '/Users/robertbiddlev/kalshi-platform/kalshi_trader.py'
code = open(path).read()

changes = 0

# 1. Add TRADE_COOLDOWN_SECONDS to Config
old = '    SCAN_INTERVAL:  int   = 60'
new = '    SCAN_INTERVAL:  int   = 60\n    TRADE_COOLDOWN_SECONDS: int = 1800  # 30 min between trades'
if old in code:
    code = code.replace(old, new)
    changes += 1
    print("1. Added TRADE_COOLDOWN_SECONDS")
else:
    print("1. ERROR: SCAN_INTERVAL not found")

# 2. Add last_trade_time to RiskManager
old = '        self.skip_counts: dict[str, int] = {}'
new = '        self.skip_counts: dict[str, int] = {}\n        self.last_trade_time: float = 0.0'
if old in code:
    code = code.replace(old, new)
    changes += 1
    print("2. Added last_trade_time to RiskManager")
else:
    print("2. ERROR: skip_counts not found")

# 3. Add cooldown check in can_trade
old = '        if sig.ticker in self.open:\n            return False, f"Already have position in {sig.ticker}"'
new = '        if sig.ticker in self.open:\n            return False, f"Already have position in {sig.ticker}"\n        cooldown_remaining = CFG.TRADE_COOLDOWN_SECONDS - (time.time() - self.last_trade_time)\n        if cooldown_remaining > 0:\n            return False, f"Cooldown: {int(cooldown_remaining)}s remaining"'
if old in code:
    code = code.replace(old, new)
    changes += 1
    print("3. Added cooldown check to can_trade")
else:
    print("3. ERROR: can_trade pattern not found")

# 4. Update last_trade_time after successful order
old = '                risk.open_position(pos)'
new = '                risk.open_position(pos)\n                risk.last_trade_time = time.time()'
if old in code:
    code = code.replace(old, new)
    changes += 1
    print("4. Added last_trade_time update after order")
else:
    print("4. ERROR: open_position not found")

# 5. Only take top 1 signal per cycle
old = '        sized_signals = liquid_signals  # v2.3: LIQUID-only execution'
new = '        sized_signals = liquid_signals[:1]  # Max 1 trade per cycle, highest score'
if old in code:
    code = code.replace(old, new)
    changes += 1
    print("5. Limited to top 1 signal per cycle")
else:
    print("5. ERROR: sized_signals not found")

print(f"\n{changes}/5 changes applied")

try:
    ast.parse(code)
    open(path, 'w').write(code)
    print("SUCCESS: syntax OK, file written")
except SyntaxError as e:
    print("SYNTAX ERROR:", e)
