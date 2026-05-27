import re, ast

path = '/Users/robertbiddlev/kalshi-platform/kalshi_trader.py'
code = open(path).read()

pattern = r'def _rsa_sign\(method: str, path: str\) -> dict:.*?return headers'

new_func = (
    'def _rsa_sign(method: str, path: str) -> dict:\n'
    '    ts = str(int(time.time() * 1000))\n'
    '    msg = ts + method.upper() + path\n'
    '    headers = {"Content-Type": "application/json", "Accept": "application/json"}\n'
    '    if not CFG.API_KEY:\n'
    '        return headers\n'
    '    try:\n'
    '        from cryptography.hazmat.primitives import hashes, serialization\n'
    '        from cryptography.hazmat.primitives.asymmetric import padding\n'
    '        import os as _os\n'
    '        kf = "/etc/secrets/kalshi_key.pem"\n'
    '        raw = open(kf).read() if _os.path.exists(kf) else CFG.API_SECRET\n'
    '        raw = raw.replace("\\\\n", chr(10))\n'
    '        pk = serialization.load_pem_private_key(raw.strip().encode(), password=None)\n'
    '        sig = pk.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())\n'
    '        import base64 as _b64\n'
    '        headers.update({"KALSHI-ACCESS-KEY": CFG.API_KEY, "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": _b64.b64encode(sig).decode()})\n'
    '    except Exception as e:\n'
    '        log.warning("RSA signing failed: %s", e)\n'
    '    return headers'
)

new_code = re.sub(pattern, new_func, code, flags=re.DOTALL)

if new_code == code:
    print('ERROR: pattern not found')
else:
    try:
        ast.parse(new_code)
        open(path, 'w').write(new_code)
        print('SUCCESS: syntax OK, file written')
    except SyntaxError as e:
        print('SYNTAX ERROR:', e)
