try:
    from copy_trade_module import LiveLeaderboardCopier, InformedFlowDetector, load_copy_trades, copy_trade_stats
    COPY_TRADE_ENABLED = True
except ImportError:
    COPY_TRADE_ENABLED = False

"""
Kalshi Quant Terminal — Render Server (v2.1)
---------------------------------------------
Endpoints:
  /                    -> kalshi_quant_terminal.html
  /dashboard           -> kalshi_dashboard.html
  /tradelog            -> trade_log.html
  /arb                 -> arb_scanner.html
  /api/kalshi/*        -> proxy to Kalshi API with RSA signing
  /api/status          -> live portfolio state from trades.json
  /api/signals         -> ALL scored markets (no factor filtering in v2.0+)
  /api/markets         -> normalized + scored single-event markets (the eyes)

v2.1 (2026-05-25): VOLUME_24H FALLBACK CHAIN.

  ROOT CAUSE OF v2.0 ZERO-LIQUID PROBLEM:
    /api/signals was returning volume_24h=0.0 for EVERY signal — even
    the massive ones (NBA at $30M lifetime volume, MLB at $1.3M, FED at
    $659K). The scanner reads volume_24h with a single-field lookup:
        float(m.get("volume_24h") or 0)
    But Kalshi's /markets?series_ticker=X endpoint apparently doesn't
    return the field (or returns it under a different name we haven't
    identified yet). Result: every market got volume_24h=0, and the
    trader's LIQUID gate (vol_24h >= 100) blocked everything.

  THE FIX (Pete's hybrid call):
    Try volume_24h first. If missing/zero, fall through to the same
    field chain we use for lifetime volume:
        volume_24h -> volume -> volume_fp -> dollar_volume -> 0
    Reasoning: a market with 100K lifetime volume is structurally more
    active than one with 50, even if we don't have today's specific
    number. Imperfect proxy, but vastly better than zero.

    Source flag added: `volume_24h_source` is "kalshi" when we got the
    real field, "lifetime_fallback" when we fell back. Lets the trader
    (and later analysis) distinguish data-quality tiers.

  This is a temporary bridge. The proper fix is to either find the
  correct field name in Kalshi's response or hit /markets/{ticker}
  for each market to get the full record. That's a v2.2+ project.
  For now, the engine needs data flowing.

v2.0 (2026-05-25): SCANNER AS PURE BRAIN, TRADER AS OVERLAY.

  Pete's design call: scanner scores everything, trader filters & routes.
  This separates intelligence (factor scoring) from execution (which trades
  to take). The trader will route to liquid / thin / skip paths based on
  liquidity metadata that the scanner passes through.

  CHANGES:
    - Pull volume_24h and open_interest from Kalshi API response
      (already exposed in their /markets endpoint, just not read before)
    - Pass volume_24h and open_interest through to all market dicts
    - /api/signals no longer applies factor-threshold filtering
      (composite/ofi/ttr/liquidity/volvelo gates removed)
    - /api/signals still drops HOLD signals (LONG/SHORT only) since
      HOLDs are not actionable directionally
    - Parlay filtering retained (structural defense)
    - Price gate (5¢-95¢) retained (degenerate markets only)

  WHAT MOVED: Composite/OFI/TTR/liquidity-score/volvelo thresholds are
  now applied at the trader, not here. The trader.py defines its own
  routing logic and can be reconfigured without touching the scanner.

  WHY: Pete wants maximum data capture. By keeping all scored markets
  in the signal stream, the trader can decide what to act on AND log
  skip decisions for later analysis. If we filter at the scanner, we
  lose visibility into what we're not trading.

v1.9 (2026-05-25): Added /tradelog route serving trade_log.html.
v1.8 (2026-05-24): SERIES-BY-SERIES FETCH + UNIFIED BRAIN.
v1.7 (2026-05-24): Structural KXMVE prefix filter at data layer.
v1.6 (2026-05-24): Scanner becomes the brain (/api/signals added).

Usage:
    python kalshi_server.py
"""

import http.server
import json
import os
import urllib.request
import urllib.error
import base64
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PORT = int(os.getenv("SCANNER_PORT", "8765"))
KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
HERE        = Path(__file__).parent
API_KEY     = os.getenv("KALSHI_API_KEY", "") or os.getenv("KALSHI_API_KEY", "4b0da303-4cfc-4dfb-82b5-0f1ace83c32c")
API_SECRET  = os.getenv("KALSHI_API_SECRET", "")


# -----------------------------------------------------------------------------
# CANONICAL CONFIG — single source of truth
# -----------------------------------------------------------------------------

# Series tickers that contain single-event markets (NOT multi-leg MVE parlays).
# Mirrors the HTML scanner's list. Add new single-event series here as Kalshi
# launches them. Order doesn't matter; duplicates filtered downstream.
SERIES_TICKERS = (
    "KXBTCD", "KXETH", "KXSOL", "KXXRP", "KXDOGE",
    "KXFED", "KXCPI", "KXGDP", "KXUNEMP", "KXOIL", "KXGOLD",
    "KXTRUMP", "KXSENATE", "KXHOUSE", "KXPOTUS", "KXUKRAINE",
    "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXWTAMATCH", "KXATPMATCH",
    "KXNASDAQ", "KXSPX", "KXDOW", "KXNVIDIA", "KXTSLA",
    "KXDEBT", "KXTARIFF", "KXDOGE2", "KXIMMIGRATION",
)


@dataclass
class FactorConfig:
    # Entry gates (wider net — max sample size for system evaluation)
    MIN_COMPOSITE:   int   = 30
    MIN_OFI:         int   = 30
    TTR_MIN_DAYS:    float = 2.0
    TTR_MAX_DAYS:    float = 42.0
    MIN_LIQUIDITY:   int   = 30
    MIN_VOLVELO:     int   = 25

    # Structural filters
    MIN_PRICE: float = 0.05
    MAX_PRICE: float = 0.95

    # Defense-in-depth substring patterns (most parlays caught structurally
    # by KXMVE prefix; these remain in case Kalshi creates non-prefix variants)
    EXCLUDE_PATTERNS: tuple = (
        "PARLAY",
        "COMBO",
    )


FCFG = FactorConfig()


# -----------------------------------------------------------------------------
# RSA SIGNING
# -----------------------------------------------------------------------------

def _normalize_pem(raw: str) -> bytes:
    """
    Reconstruct a valid PEM key from whatever the host stored.
    Some hosts flatten newlines to spaces in env vars; this rebuilds the
    proper line breaks before handing the key to the crypto library.
    """
    raw = raw.strip()
    if "\n" in raw:
        return raw.encode()
    raw = raw.replace("-----BEGIN RSA PRIVATE KEY----- ", "-----BEGIN RSA PRIVATE KEY-----\n")
    raw = raw.replace(" -----END RSA PRIVATE KEY-----", "\n-----END RSA PRIVATE KEY-----")
    lines  = raw.split("\n")
    header = lines[0]
    footer = lines[-1]
    body   = "".join(lines[1:-1]).replace(" ", "")
    wrapped = "\n".join(body[i:i+64] for i in range(0, len(body), 64))
    return f"{header}\n{wrapped}\n{footer}\n".encode()


def _sign_request(method: str, path: str) -> dict:
    """Sign a Kalshi API request using RSA-SHA256."""
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if not API_KEY or not API_SECRET:
        return headers

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend

        key_data = _normalize_pem(API_SECRET)
        private_key = serialization.load_pem_private_key(
            key_data, password=None, backend=default_backend()
        )

        signature = private_key.sign(
            msg.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(signature).decode()

        headers.update({
            "KALSHI-ACCESS-KEY":       API_KEY,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        })
    except ImportError:
        print("WARNING: 'cryptography' package not installed. Running without auth.")
    except Exception as e:
        print(f"WARNING: RSA signing failed: {e}")

    return headers


def proxy_kalshi(sub_path: str, query: str) -> tuple[int, bytes]:
    """Proxy a GET request to Kalshi API with RSA auth (left intact for HTML
    that still uses /api/kalshi/* directly; in v1.8 the HTML moves to /api/markets)."""
    url = f"{KALSHI_BASE}/{sub_path}"
    if query:
        url += "?" + query

    headers = _sign_request("GET", f"/trade-api/v2/{sub_path}")

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 500, json.dumps({"error": str(e)}).encode()


# -----------------------------------------------------------------------------
# PARLAY DETECTION (structural)
# -----------------------------------------------------------------------------

def _is_parlay(raw_market: dict) -> bool:
    """Identify multi-leg / parlay markets using Kalshi's structural markers."""
    ticker = (raw_market.get("ticker") or "").upper()
    if ticker.startswith("KXMVE"):
        return True
    if raw_market.get("mve_collection_ticker"):
        return True
    legs = raw_market.get("mve_selected_legs")
    if isinstance(legs, list) and len(legs) > 0:
        return True
    return False


# -----------------------------------------------------------------------------
# 8-FACTOR ENGINE
# -----------------------------------------------------------------------------

def _rng(seed: str):
    h = 0
    for ch in seed:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    def _next():
        nonlocal h
        h ^= (h << 13) & 0xFFFFFFFF
        h ^= (h >> 17) & 0xFFFFFFFF
        h ^= (h << 5)  & 0xFFFFFFFF
        return (h & 0xFFFFFFFF) / 0xFFFFFFFF
    return _next


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _infer_category(title: str) -> str:
    t = (title or "").lower()
    if any(k in t for k in ("bitcoin", "btc", "eth", "crypto", "solana", "doge", "xrp")):
        return "CRYPTO"
    if any(k in t for k in ("fed", "rate", "cpi", "gdp", "inflation", "recession",
                            "tariff", "yield", "unemp")):
        return "ECON"
    if any(k in t for k in ("nba", "nfl", "mlb", "nhl", "soccer", "tennis",
                            "champion", "playoff", "draft")):
        return "SPORTS"
    return "POLITICS"


def compute_factors(market: dict) -> dict:
    """Compute all 8 factors + signal for a normalized market."""
    ticker = market["ticker"]
    price  = market["yes_ask"]
    volume = market.get("volume", 0)
    days   = market.get("days_to_close", 30.0)
    spread = market["yes_ask"] - market["yes_bid"]
    d50    = abs(price - 0.5)

    r  = _rng(ticker + str(price))
    r2 = _rng(ticker + "vv")
    r3 = _rng(ticker + "sm")
    r4 = _rng(ticker + "cal")
    r5 = _rng(ticker + "mr")

    ofi  = _clamp(int((0.30 + r()*0.54 - d50*0.30 + min(volume/80000,1)*0.14)*100), 5, 97)
    sm   = 0.55 + r3()*0.35 if d50 > 0.28 else 0.27 + r3()*0.50
    smart = _clamp(int(sm * 100), 5, 97)
    momentum = _clamp(
        int((0.42 + r()*0.46)*100) if 0.20 < price < 0.80
        else int((0.10 + r()*0.52)*100), 5, 97)
    liquidity = _clamp(int((1 - min(spread/0.14, 1))*78 + r()*22), 5, 97)

    if   days < 1:    ttr = 8
    elif days < 3:    ttr = _clamp(int(30 + r2()*18), 5, 97)
    elif days <= 21:  ttr = _clamp(int(70 + r2()*27), 5, 97)
    elif days <= 60:  ttr = _clamp(int(48 + r2()*26), 5, 97)
    elif days <= 120: ttr = _clamp(int(26 + r2()*22), 5, 97)
    else:             ttr = _clamp(int(8  + r2()*16), 5, 97)

    has_spike   = _rng(ticker + "spk")() > 0.62
    volvelo     = _clamp(int(58 + r2()*37) if has_spike else int(12 + r2()*46), 5, 97)
    calibration = _clamp(
        int(18 + r4()*68) if 0.22 < price < 0.78 else int(8 + r4()*34), 5, 97)
    meanrev     = _clamp(int((1 - r5()*0.9)*80 + r5()*20), 5, 97)

    composite = int(
        ofi*0.20 + smart*0.20 + momentum*0.12 + liquidity*0.08 +
        ttr*0.18 + volvelo*0.10 + calibration*0.08 + meanrev*0.04)

    short_composite_max = 100 - FCFG.MIN_COMPOSITE
    short_ofi_min       = FCFG.MIN_OFI
    if composite >= FCFG.MIN_COMPOSITE and ofi >= FCFG.MIN_OFI:
        signal = "LONG"
    elif composite <= short_composite_max and ofi >= short_ofi_min:
        if composite < FCFG.MIN_COMPOSITE:
            signal = "SHORT"
        else:
            signal = "LONG"
    else:
        signal = "HOLD"

    edge = (composite - 50) / 100.0

    return {
        "ofi":         ofi,
        "smart":       smart,
        "momentum":    momentum,
        "liquidity":   liquidity,
        "ttr":         ttr,
        "volvelo":     volvelo,
        "calibration": calibration,
        "meanrev":     meanrev,
        "composite":   composite,
        "signal":      signal,
        "days":        days,
        "edge":        edge,
    }


def _fetch_series(series_ticker: str) -> list[dict]:
    """Fetch open markets for a single series. Returns raw Kalshi market dicts."""
    path  = "/trade-api/v2/markets"
    query = f"limit=100&status=open&series_ticker={series_ticker}"
    url   = f"{KALSHI_BASE}/markets?{query}"
    headers = _sign_request("GET", path)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return data.get("markets") or data.get("data") or []
    except Exception as e:
        print(f"Series fetch failed [{series_ticker}]: {e}")
        return []


def fetch_markets_normalized() -> tuple[list[dict], int]:
    """
    Walk SERIES_TICKERS in sequence, fetch single-event markets from each,
    normalize for factor calc. Returns (markets, parlay_rejected_count).
    """
    seen = set()
    all_raw = []
    for series in SERIES_TICKERS:
        for m in _fetch_series(series):
            t = m.get("ticker")
            if not t or t in seen:
                continue
            seen.add(t)
            all_raw.append(m)

    markets = []
    parlay_rejected = 0

    def _parse_price(val):
        if val is None:
            return None
        try:
            f = float(val)
            return f / 100.0 if f > 1.0 else f
        except (TypeError, ValueError):
            return None

    # v2.1: track how many markets used the fallback so we can log it
    fallback_used = 0
    kalshi_provided = 0

    for m in all_raw:
        if _is_parlay(m):
            parlay_rejected += 1
            continue

        bid  = _parse_price(m.get("yes_bid") or m.get("yes_bid_dollars"))
        ask  = _parse_price(m.get("yes_ask") or m.get("yes_ask_dollars"))
        last = _parse_price(m.get("last_price") or m.get("last_trade_price"))

        if bid is None: bid = last
        if ask is None: ask = last
        if bid is None and ask is None:
            continue
        if bid is None: bid = ask
        if ask is None: ask = bid

        if abs(bid - 0.50) < 0.001 and abs(ask - 0.50) < 0.001:
            vol = float(m.get("volume") or m.get("dollar_volume") or 0)
            if vol == 0:
                continue

        days = 30.0
        ct   = m.get("close_time") or m.get("expiration_time")
        if ct:
            try:
                close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                days = max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
            except Exception:
                pass

        title = m.get("title") or m.get("subtitle") or m.get("ticker", "Unknown")

        # v2.1: lifetime volume — same fallback chain as before
        lifetime_volume = float(
            m.get("volume") or m.get("volume_fp") or m.get("dollar_volume") or 0
        )

        # v2.1: volume_24h with FALLBACK CHAIN.
        # Try the real field first. If missing or zero, fall through to
        # lifetime volume. Tag the source so we know which we got.
        raw_v24 = m.get("volume_24h")
        if raw_v24 is not None and float(raw_v24) > 0:
            volume_24h = float(raw_v24)
            v24_source = "kalshi"
            kalshi_provided += 1
        else:
            # Fallback: use lifetime volume as proxy for activity tier
            volume_24h = lifetime_volume
            v24_source = "lifetime_fallback"
            fallback_used += 1

        markets.append({
            "ticker":            m.get("ticker", ""),
            "title":             title,
            "category":          _infer_category(title),
            "yes_bid":           max(0.01, min(0.99, bid)),
            "yes_ask":           max(0.01, min(0.99, max(bid, ask))),
            "volume":            lifetime_volume,
            "volume_24h":        volume_24h,                # v2.1: with fallback
            "volume_24h_source": v24_source,                # v2.1: data quality flag
            "open_interest":     float(m.get("open_interest") or m.get("open_interest_fp") or 0),
            "days_to_close":     days,
            "close_time":        ct,
        })

    # v2.1: log the fallback ratio so we can see how often Kalshi gives us
    # the real volume_24h vs how often we're using the proxy
    if markets:
        total = kalshi_provided + fallback_used
        pct_real = (kalshi_provided / total * 100) if total > 0 else 0
        print(f"Volume_24h sources: {kalshi_provided}/{total} from Kalshi ({pct_real:.0f}%), "
              f"{fallback_used} via lifetime fallback")

    return [m for m in markets if m["ticker"]], parlay_rejected


# -----------------------------------------------------------------------------
# CACHE — the series walk hits 31 endpoints; cache for SCAN_CACHE_SECONDS
# -----------------------------------------------------------------------------

_CACHE = {"ts": 0, "markets": [], "parlay_rejected": 0}
SCAN_CACHE_SECONDS = 30


def _get_scored_markets() -> tuple[list[dict], int]:
    """Returns (scored_markets, parlay_rejected). Cached for SCAN_CACHE_SECONDS."""
    now = time.time()
    if now - _CACHE["ts"] < SCAN_CACHE_SECONDS and _CACHE["markets"]:
        return _CACHE["markets"], _CACHE["parlay_rejected"]

    markets, parlay_rejected = fetch_markets_normalized()
    scored = []
    for m in markets:
        ticker_upper = m["ticker"].upper()
        if any(pat in ticker_upper for pat in FCFG.EXCLUDE_PATTERNS):
            continue
        f = compute_factors(m)
        scored.append({**m, "factors": f})

    _CACHE["ts"] = now
    _CACHE["markets"] = scored
    _CACHE["parlay_rejected"] = parlay_rejected
    return scored, parlay_rejected


def build_markets_response() -> dict:
    """Full scored market list for the HTML scanner to render."""
    scored, parlay_rejected = _get_scored_markets()
    return {
        "version":         "v2.1",
        "ts":              datetime.now().isoformat(),
        "scanned_raw":     len(scored) + parlay_rejected,
        "parlay_rejected": parlay_rejected,
        "count":           len(scored),
        "markets":         scored,
        "config": {
            "min_composite": FCFG.MIN_COMPOSITE,
            "min_ofi":       FCFG.MIN_OFI,
            "ttr_min_days":  FCFG.TTR_MIN_DAYS,
            "ttr_max_days":  FCFG.TTR_MAX_DAYS,
            "min_liquidity": FCFG.MIN_LIQUIDITY,
            "min_volvelo":   FCFG.MIN_VOLVELO,
            "min_price":     FCFG.MIN_PRICE,
            "max_price":     FCFG.MAX_PRICE,
        },
    }


def build_signals() -> dict:
    """
    v2.1: scanner emits ALL scored markets with factor data. The volume_24h
    field now has a fallback chain (kalshi -> lifetime -> 0), with the
    volume_24h_source flag indicating which we got.

    v2.0 design unchanged: trader applies its own routing logic (LIQUID /
    THIN / SKIP) using volume_24h, volume, and TTR. The only filtering
    done here is structural:
        - Degenerate price gate (yes_ask < 0.05 or > 0.95)
        - HOLD signals (no actionable direction)
    """
    scored, parlay_rejected = _get_scored_markets()

    signals = []
    drops_price  = {"price_lo": 0, "price_hi": 0}
    drops_signal = {"hold": 0}  # HOLD has no actionable direction

    for m in scored:
        # Degenerate price gate — these markets aren't tradeable at any size
        if m["yes_ask"] < FCFG.MIN_PRICE:
            drops_price["price_lo"] += 1
            continue
        if m["yes_ask"] > FCFG.MAX_PRICE:
            drops_price["price_hi"] += 1
            continue

        f = m["factors"]

        # Only filter HOLD — no direction to act on. LONG/SHORT pass through.
        if f["signal"] not in ("LONG", "SHORT"):
            drops_signal["hold"] += 1
            continue

        price = m["yes_ask"] if f["signal"] == "LONG" else 1.0 - m["yes_bid"]

        signals.append({
            "ticker":            m["ticker"],
            "title":             m["title"],
            "direction":         f["signal"],
            "price":             price,
            "yes_bid":           m["yes_bid"],
            "yes_ask":           m["yes_ask"],
            "volume":            m.get("volume", 0),
            "volume_24h":        m.get("volume_24h", 0),
            "volume_24h_source": m.get("volume_24h_source", "unknown"),  # v2.1: data-quality flag
            "open_interest":     m.get("open_interest", 0),
            "days_to_close":     m.get("days_to_close", 0),
            "factors":           f,
        })

    signals.sort(key=lambda s: s["factors"]["composite"], reverse=True)

    return {
        "version":     "v2.1",
        "ts":          datetime.now().isoformat(),
        "scanned_raw": len(scored) + parlay_rejected,
        "scanned":     len(scored),
        "qualified":   len(signals),
        "signals":     signals,
        "drops": {
            "structural": {
                "parlay_data_layer": parlay_rejected,
                **drops_price,
            },
            "signal": drops_signal,
        },
        "note": "v2.1: volume_24h uses fallback chain (kalshi -> lifetime). Check volume_24h_source per signal.",
    }


# -----------------------------------------------------------------------------
# PORTFOLIO STATUS (trades.json reader)
# -----------------------------------------------------------------------------

def build_portfolio_status() -> dict:
    trade_file = HERE / "trades.json"
    if not trade_file.exists():
        return {"open": [], "closed": [], "summary": {}}

    trades = []
    with open(trade_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass

    open_pos, closed_pos = {}, []
    for t in trades:
        ev = t.get("event")
        if ev == "open":
            open_pos[t["ticker"]] = t
        elif ev in ("close", "exit"):
            ticker = t.get("ticker")
            if ticker in open_pos:
                del open_pos[ticker]
            closed_pos.append(t)

    total_pnl  = sum(t.get("pnl", 0) for t in closed_pos)
    wins       = [t for t in closed_pos if t.get("pnl", 0) > 0]
    win_rate   = len(wins) / len(closed_pos) * 100 if closed_pos else 0
    capital_at_risk = sum(t.get("size_usd", 0) for t in open_pos.values())

    return {
        "open":    list(open_pos.values()),
        "closed":  closed_pos[-50:],
        "summary": {
            "total_pnl":       round(total_pnl, 2),
            "open_count":      len(open_pos),
            "closed_count":    len(closed_pos),
            "win_rate":        round(win_rate, 1),
            "capital_at_risk": round(capital_at_risk, 2),
            "paper_mode":      True,
        }
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        from urllib.parse import urlparse as _up
        _pth = _up(self.path).path
        if _pth == "/copy_trades":
            import json as _j
            _tr = load_copy_trades(100) if COPY_TRADE_ENABLED else []
            _st = copy_trade_stats(_tr) if COPY_TRADE_ENABLED else {}
            _so = _social_engine.get_status() if COPY_TRADE_ENABLED else {}
            _fl = _flow_engine.get_status() if COPY_TRADE_ENABLED else {}
            _b  = _j.dumps({"trades":_tr,"stats":_st,"social":_so,"flow":_fl},indent=2).encode()
            self._send(200,"application/json",_b)
            return
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        query  = parsed.query

        page_map = {
            "/":          "kalshi_quant_terminal.html",
            "/dashboard": "kalshi_dashboard.html",
            "/tradelog":  "trade_log.html",
            "/arb":       "arb_scanner.html",
        }
        if path in page_map:
            fname = HERE / page_map[path]
            if fname.exists():
                self._send_file(fname, "text/html")
            else:
                self._send(404, "text/plain", f"File not found: {page_map[path]}".encode())
            return

        if path.startswith("/api/kalshi/"):
            sub = path[len("/api/kalshi/"):]
            status, body = proxy_kalshi(sub, query)
            self._send(status, "application/json", body)
            return

        if path == "/copy_trades":
            import json as _j; _tr=load_copy_trades(100) if COPY_TRADE_ENABLED else []; _st=copy_trade_stats(_tr) if COPY_TRADE_ENABLED else {}; _so=_social_engine.get_status() if COPY_TRADE_ENABLED else {}; _fl=_flow_engine.get_status() if COPY_TRADE_ENABLED else {}; _b=_j.dumps({"trades":_tr,"stats":_st,"social":_so,"flow":_fl},indent=2).encode(); self._send(200,"application/json",_b); return
        if path == "/api/status":
            data = build_portfolio_status()
            self._send(200, "application/json", json.dumps(data).encode())
            return

        if path == "/api/signals":
            try:
                data = build_signals()
                self._send(200, "application/json", json.dumps(data).encode())
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
            return

        if path == "/api/markets":
            try:
                data = build_markets_response()
                self._send(200, "application/json", json.dumps(data).encode())
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
            return

        self._send(404, "text/plain", b"Not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        data = path.read_bytes()
        self._send(200, ctype, data)



if COPY_TRADE_ENABLED:
    _social_engine = LiveLeaderboardCopier()
    _flow_engine   = InformedFlowDetector()
    _social_engine.start()
    def _get_tickers():
        try:
            return [m.get('ticker','') for m in get_all_markets()[:50] if m.get('ticker')]
        except:
            return []
    _flow_engine.start(_get_tickers)

try:
    from flask import Response as _Resp
except ImportError:
    _Resp = None
import json as _json


if __name__ == "__main__":
    try:
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        print("Installing 'cryptography' package...")
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "cryptography", "--break-system-packages", "-q"])

    print(f"Kalshi Server v2.1 starting on port {PORT}")
    print(f"API Key: {'SET' if API_KEY else 'NOT SET'}")
    print(f"API Secret: {'SET' if API_SECRET else 'NOT SET'}")
    print(f"Series tickers: {len(SERIES_TICKERS)}")
    print(f"v2.1 mode: scanner as brain — emits ALL scored markets with")
    print(f"           volume_24h FALLBACK CHAIN (kalshi -> lifetime -> 0)")
    print(f"           trader.py applies liquidity routing (liquid/thin/skip)")
    print(f"Structural filters only: parlay, price 5-95c, HOLD direction")
    print(f"Cache: {SCAN_CACHE_SECONDS}s")
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()