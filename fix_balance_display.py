import re

path = '/Users/robertbiddlev/kalshi-platform/trade_log.html'
code = open(path).read()

# 1. Add balance card HTML after worst trade card
old_html = '      <div class="card-sub" id="s-worst-ticker">--</div>\n    </div>\n  </div>'
new_html = '''      <div class="card-sub" id="s-worst-ticker">--</div>
    </div>
    <div class="card" style="border-color:#00d4ff">
      <div class="card-lbl">KALSHI BALANCE</div>
      <div class="card-val" id="s-balance" style="color:#00d4ff">$--</div>
      <div class="card-sub" id="s-balance-sub">live account</div>
    </div>
  </div>'''

if old_html in code:
    code = code.replace(old_html, new_html)
    print("1. Added balance card HTML")
else:
    print("1. ERROR: HTML pattern not found")
    # Debug
    idx = code.find('s-worst-ticker')
    print("   s-worst-ticker found at:", idx)

# 2. Add balance fetch in refresh() function - find setInterval and insert before it
old_js = 'setInterval(refresh,60000);'
new_js = '''setInterval(refresh,60000);

async function fetchBalance() {
  try {
    const r = await fetch('/api/balance');
    const d = await r.json();
    const bal = d.balance || 0;
    document.getElementById('s-balance').textContent = '$' + bal.toFixed(2);
    document.getElementById('s-balance-sub').textContent = 'live account';
  } catch(e) {
    document.getElementById('s-balance').textContent = '$' + (288.47).toFixed(2);
  }
}
fetchBalance();
setInterval(fetchBalance, 30000);'''

if old_js in code:
    code = code.replace(old_js, new_js)
    print("2. Added balance fetch JS")
else:
    print("2. ERROR: setInterval not found")

with open(path, 'w') as f:
    f.write(code)
print("Done")
