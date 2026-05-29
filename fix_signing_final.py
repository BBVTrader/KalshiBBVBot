#!/usr/bin/env python3
import re, subprocess, os

os.chdir(os.path.expanduser("~/kalshi-platform"))
content = open('kalshi_trader.py').read()

# Replace entire _rsa_sign function with clean version
# Find start and end of function
start = content.find('def _rsa_sign(method: str, path: str) -> dict:')
if start == -1:
    print("ERROR: _rsa_sign not found")
    exit(1)

# Find the next top-level function definition after _rsa_sign
next_func = re.search(r'\ndef [a-z]', content[start+10:])
if not next_func:
    print("ERROR: Could not find end of function")
    exit(1)

end = start + 10 + next_func.start() + 1
print(f"Found _rsa_sign from char {start} to {end}")
print(f"Function length: {end-start} chars")

new_func = '''def _rsa_sign(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not CFG.API_KEY:
        return headers
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64 as _b64, os as _os
        raw = CFG.API_SECRET
        # Handle literal \\n from Render env vars
        raw = raw.replace("\\\\n", "\\n").replace("\\\\r", "").strip()
        # If no real newlines, use normalize
        if "\\n" not in raw:
            raw = _normalize_pem(raw).decode()
        pk = serialization.load_pem_private_key(raw.encode(), password=None)
        sig = pk.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        headers.update({
            "KALSHI-ACCESS-KEY": CFG.API_KEY,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": _b64.b64encode(sig).decode()
        })
    except Exception as e:
        log.warning("RSA signing failed: %s", e)
    return headers

'''

new_content = content[:start] + new_func + content[end:]

# Verify syntax
import ast
try:
    ast.parse(new_content)
    print("Syntax OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    exit(1)

# Verify only one _rsa_sign now
count = new_content.count('def _rsa_sign')
print(f"_rsa_sign count: {count}")

open('kalshi_trader.py', 'w').write(new_content)
print("File written")

# Show the new function
start2 = new_content.find('def _rsa_sign')
end2 = new_content.find('\ndef ', start2+10)
print("\nNew function:")
print(new_content[start2:end2])

subprocess.run(['git', 'add', 'kalshi_trader.py'])
r = subprocess.run(['git', 'commit', '-m', 'fix: clean single RSA sign function with proper newline handling'],
    capture_output=True, text=True)
print(r.stdout or r.stderr)
r2 = subprocess.run(['git', 'push'], capture_output=True, text=True)
print(r2.stdout or r2.stderr)
