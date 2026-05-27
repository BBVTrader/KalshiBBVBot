import re

path = '/Users/robertbiddlev/kalshi-platform/kalshi_trader.py'
code = open(path).read()

# Replace the entire _rsa_sign function with a clean version
pattern = r'def _rsa_sign\(method: str, path: str\) -> dict:.*?return headers'

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
        key_file = "/etc/secrets/kalshi_key.pem"
        if _os.path.exists(key_file):
            raw = open(key_file).read()
        else:
            raw = CFG.API_SECRET
        # Convert escaped newlines to real newlines
        raw = raw.replace('\\\\n', chr(10)).replace('\\n', chr(10))
        private_key = serialization.load_pem_private_key(raw.strip().encode(), password=None)
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

new_code = re.sub(pattern, new_func, code, flags=re.DOTALL)

if new_code != code:
    open(path, 'w').write(new_code)
    # Verify no syntax errors
    import ast
    try:
        ast.parse(new_code)
        print('SUCCESS: syntax OK')
    except SyntaxError as e:
        print('SYNTAX ERROR:', e)
else:
    print('ERROR: pattern not found')
