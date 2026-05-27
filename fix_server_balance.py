path = '/Users/robertbiddlev/kalshi-platform/kalshi_server.py'
code = open(path).read()

old = '        if path == "/api/status":'
new = '''        if path == "/api/balance":
            try:
                headers = _sign_request("GET", "/trade-api/v2/portfolio/balance")
                req = urllib.request.Request(f"{KALSHI_BASE}/portfolio/balance", headers=headers)
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                bal = data.get("balance", 0) / 100.0
                self._send(200, "application/json", json.dumps({"balance": bal}).encode())
            except Exception as e:
                self._send(200, "application/json", json.dumps({"balance": 0, "error": str(e)}).encode())
            return

        if path == "/api/status":'''

if old in code:
    open(path, 'w').write(code.replace(old, new))
    print("SUCCESS")
else:
    print("ERROR: pattern not found")
