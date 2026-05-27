import re

code = open('/Users/robertbiddlev/kalshi-platform/kalshi_trader.py').read()

# Find and replace the entire _rsa_sign function
new_func = '''def _rsa_sign(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not CFG.API_KEY:
        return headers
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import os as _os
        _key_file = "/etc/secrets/kalshi_key.pem"
        if _os.path.exists(_key_file):
            pem_bytes = open(_key_file, "rb").read()
        else:
            raw = CFG.API_SECRET
            # Render stores env vars with literal \\n - convert to real newlines
            raw = raw.replace('\\\\n', '\\n')
            if '\\n' not in raw and 'BEGIN' in raw:
                pass  # already has real newlines
            pem_bytes = raw.encode()
        private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        sig = private_key.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256()
        )
        headers.update({
            "KALSHI-ACCESS-KEY": CFG.API_KEY,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        })
    except Exception as e:
        log.warning("RSA signing failed: %s", e)
    return headers'''

# Replace the function using regex
pattern = r'def _rsa_sign\(method: str, path: str\) -> dict:.*?return headers'
new_code = re.sub(pattern, new_func, code, flags=re.DOTALL)

if new_code != code:
    open('/Users/robertbiddlev/kalshi-platform/kalshi_trader.py', 'w').write(new_code)
    print('SUCCESS: function replaced')
else:
    print('ERROR: pattern not found')
