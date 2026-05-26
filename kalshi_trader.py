"""
Kalshi Automated Trading System — Executor (v2.3)
==================================================
RSA-SHA256 signed requests for Kalshi live API.
Paper trade by default; flip PAPER_TRADE=False for live.

v2.3 (2026-05-25): WIDE-FIRST MEASUREMENT MODE.

  Pete's call: v2.2 went narrow (TTR<=3d, vol>=10K, +20%/-10% exits) and
  produced ZERO LIQUID signals across multiple cycles. In hindsight that
  was backwards — going narrow first leaves you tuning blind because
  there's no data to tune ON. Wide first generates data; tightening is
  just filtering on top of data you actually have.

  Three coordinated loosenings to get the engine started:

    1. TTR <= 3 days  →  TTR <= 14 days
       Original 3-day cap was tied to v2.0's THIN-path settlement-hold
       design. THIN execution is off in measurement mode and LIQUID has
       real exits, so the cap's original reason no longer applies. 14d
       opens up Kalshi's weekly/sports/political markets — which tend to
       be much DEEPER than the daily crypto strikes that dominated v2.2.
       Going past 14d would start measuring signal performance over
       horizons where short-term factors (OFI, smart money) decay below
       relevance — that's a different question we can answer later by
       bucketing the data we collect now.

    2. LIQUID gates 10K/500  →  2K/100
       Redefines LIQUID as "has actually traded recently with some depth"
       rather than "institutionally liquid." For flat $25 trades that's
       the realistic execution bar — we just need book enough that entry
       doesn't move the market.

    3. Profit target 20% / Stop 10%  →  10% / 5%
       Pete's read (correct): 20% on a Kalshi contract is aspirational.
       A contract at 85c hitting +20% requires reaching 1.02 (impossible).
       Mid-priced contracts (40-70c) essentially never fire the 20% target.
       10%/5% keeps the 2:1 R/R but reflects how Kalshi contracts actually
       move. Velocity decay exit unchanged.

  Everything else from v2.2 unchanged: flat $25 sizing, LIQUID-only
  execution, scoring/OFI/composite thresholds untouched, full logging of
  composite/ofi/ttr/pnl per trade for post-hoc bucket analysis.

v2.2 (2026-05-25): FLAT SIZING + LIQUID-ONLY MEASUREMENT MODE.

  Pete's call: before we can use the scoring system to drive sizing, we
  need to validate that the scores actually predict outcomes. Doing that
  with score-driven sizing is circular — if a 60-score trade wins more
  than a 50-score, but you sized the 60 at $40 and the 50 at $10, you
  can't tell whether the score is predictive or you just made more on
  bigger positions. The score has to prove itself first, in isolation.

  Three changes for this measurement run:

    1. FLAT SIZING. Every trade is FLAT_POSITION_USD ($25). Every entry
       is the same notional risk. Wins and losses speak for themselves.
       After enough samples, bucket by score and see if hit rate actually
       rises with score. THEN sizing decisions become data-driven.
       kelly_size() and size_thin_position() are left intact for easy
       revert — they're just not called.

    2. LIQUID-ONLY EXECUTION. THIN path is still routed and counted, but
       its signals don't enter execution. Thin markets corrupt the data
       at any size — slippage, partial fills, weird settlements — and we
       can't separate "score predictiveness" from "execution noise."
       Trade only where execution is clean and we isolate the variable
       we're measuring. Skipped THIN signals are counted under
       'thin_path_disabled_for_experiment' so we can see what we passed.

    3. LIQUID GATES LOOSENED. Current production gates (vol >= 100K,
       v24h >= 5K) have produced ZERO LIQUID signals across 248 cycles.
       Loosened to vol >= 10K, v24h >= 500. The criterion we actually
       care about is "deep enough that flat $25 won't move the market" —
       these numbers reflect that bar, not the v2.0 institutional bar.
       TTR <= 3 days unchanged.

  Explicitly DEFERRED (revisit after scoring is validated):
    - Concentration caps (per-underlying, per-resolution-date)
    - Tiered or conviction-based sizing
    - Threshold optimization on composite/OFI/TTR
    - THIN path re-enablement

  Exits unchanged: 20% profit, 10% stop loss, velocity decay on LIQUID
  positions past the 30-min grace window.

  Logging unchanged: every open records composite, ofi, ttr_days; every
  close records pnl + reason. Post-hoc bucket analysis is already enabled.

v2.1 (2026-05-25): INDEPENDENT GATE EVALUATION.

  Instrumentation fix on top of v2.0 routing. Previously route_signal()
  returned on first gate failure — so a market that failed both TTR AND
  freshness only got logged as TTR. That made skip_counts misleading:
  loosening TTR would unlock some markets, but most of those would then
  also fail freshness, hiding the true binding constraint.

  v2.1 evaluates ALL gates independently. skip_reason becomes a comma-
  joined list of every failure, and record_skip() splits and tallies
  each separately. skip_counts now shows the TRUE distribution of why
  signals are being blocked, letting Pete loosen the right knob first.

  Also added generic bucketing: 'ttr_180d_gt_3d' and 'ttr_4.2d_gt_3d'
  both roll into 'ttr_gt_max', so skip_counts stays compact and summable.

v2.0 (2026-05-25): LIQUIDITY ROUTING.

  The scanner (v2.0) now emits ALL scored markets without applying factor
  thresholds. This trader applies the routing layer: every signal goes
  through a LIQUID / THIN / SKIP decision based on volume and TTR.

  Routing paths:
    LIQUID — volume >= 100K AND volume_24h >= 5K AND TTR <= 3 days
      → size = half-Kelly, capped at $200
      → exits: profit target (+20%) / stop loss (-10%) / velocity decay
                (current_volume_24h < 50% of entry value, after 30min grace)

    THIN — volume < 100K AND TTR <= 3 days AND thin_book < 20% of capital
      → size = min(half-Kelly, 5% × volume × price)
      → no stops/targets, hold to settlement (Kalshi resolves it for us)

    SKIP — everything else, logged with reason

v1.9 (2026-05-25): MARK-TO-MARKET PIPELINE FIX.
v1.8 (2026-05-24): Widened paper-mode params for sample accumulation.
v1.6 (2026-05-24): Executor-only architecture.
v1.5 (2026-05-24): Half-of-v1.1 entry gates + faster exit cycle.
v1.4 (2026-05-24): Classifier fix — compute_factors read from CFG.
v1.3 (2026-05-21): CROSSCATEGORY parlay filter.
v1.2 (2026-05-21): Orphan logic fix + structural filters.
v1.1 (2026-05-21): Wider net for sample-size accumulation.

Run: python kalshi_trader.py
"""

import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import urllib.request
import urllib.error

# =============================================================================
# PAPER_PARAMS — widened for sample accumulation (v1.8).
# Each line shows the live-recommended value as a comment.
# To go live: revert each value to the comment-suggested number.
# =============================================================================

PAPER_MAX_OPEN          = 50        # LIVE: 8
PAPER_MIN_POSITION_USD  = 5.0       # LIVE: 25.0
PAPER_MAX_POSITION_USD  = 200.0     # LIVE: 500.0
PAPER_TOTAL_CAPITAL     = 50_000.0  # LIVE: 10_000.0
PAPER_MAX_DAILY_LOSS    = 5_000.0   # LIVE: 300.0

# =============================================================================
# v2.3 MEASUREMENT MODE — flat sizing, LIQUID-only execution
# =============================================================================

FLAT_POSITION_USD     = float(os.getenv("FLAT_POSITION_USD", "25.0"))   # Every trade is this size. No exceptions.
ENABLE_THIN_EXECUTION = False  # If False, THIN signals are routed but skipped

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class Config:
    PAPER_TRADE: bool = os.getenv("PAPER_TRADE","True").lower() not in ("false","0","no")

    API_KEY:    str = os.getenv("KALSHI_API_KEY", "")
    API_SECRET: str = os.getenv("KALSHI_API_SECRET", "")
    KALSHI_BASE: str = "https://api.elections.kalshi.com/trade-api/v2"

    # Scanner is the brain — trader polls it for signals
    SCANNER_URL: str = os.getenv("SCANNER_URL", "https://kalshi-trader-1.onrender.com")

    # Exit rules — v2.3: tightened from 20%/10% to 10%/5% (2:1 R/R preserved)
    PROFIT_TARGET:   float = 0.10
    STOP_LOSS:       float = 0.05

    # Sizing — v2.3 uses FLAT_POSITION_USD; kelly fields kept for revert path
    KELLY_FRACTION:   float = 0.50
    MAX_POSITION_USD: float = PAPER_MAX_POSITION_USD
    MIN_POSITION_USD: float = PAPER_MIN_POSITION_USD

    # Risk management — values pulled from PAPER_PARAMS block above
    TOTAL_CAPITAL:  float = PAPER_TOTAL_CAPITAL
    MAX_OPEN:       int   = PAPER_MAX_OPEN
    MAX_DAILY_LOSS: float = PAPER_MAX_DAILY_LOSS
    SCAN_INTERVAL:  int   = 60

    ORPHAN_TIMEOUT_HOURS: float = 36.0

    # =========================================================================
    # LIQUIDITY ROUTING — v2.3 wide-first measurement gates
    #
    # v2.0 production: 100K / 5K / 3d  → 0 LIQUID signals
    # v2.2 first loosen: 10K / 500 / 3d  → still 0 LIQUID signals
    # v2.3 wide-first: 2K / 100 / 14d  → goal is to generate data
    # =========================================================================

    # Universal entry gate — v2.3: opened from 3d to 14d
    MAX_TTR_DAYS: float = 14.0

    # LIQUID path: deep markets with normal flow exits
    # v2.3: loosened from 10K / 500 to 2K / 100
    LIQUID_VOLUME_MIN:       float = 2_000.0     # lifetime contracts traded
    LIQUID_VOLUME_24H_MIN:   float = 100.0       # last 24hr (freshness check)
    VELOCITY_DECAY_GRACE_S:  float = 30 * 60     # 30min grace after entry
    VELOCITY_DECAY_THRESHOLD: float = 0.50       # exit if v24h < 50% of entry

    # THIN path: small markets, no exits, hold to resolution
    # v2.3: still routed and counted, but ENABLE_THIN_EXECUTION=False
    # means these never reach order placement. Skip-counted for visibility.
    THIN_CAPITAL_PCT:  float = 0.20  # max 20% of capital in thin-book
    THIN_SIZE_PCT_OF_VOLUME: float = 0.05  # max 5% of contract volume per pos

    LOG_FILE:  str = "kalshi_trader.log"
    TRADE_LOG: str = "trades.json"


CFG = Config()


# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
_fh = logging.FileHandler(CFG.LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_sh, _fh])
log = logging.getLogger("trader")


# -----------------------------------------------------------------------------
# RSA SIGNING (only for order placement — market reads are PUBLIC, v1.9)
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
    lines = raw.split("\n")
    header = lines[0]
    footer = lines[-1]
    body   = "".join(lines[1:-1]).replace(" ", "")
    wrapped = "\n".join(body[i:i+64] for i in range(0, len(body), 64))
    pem = f"{header}\n{wrapped}\n{footer}\n"
    return pem.encode()


def _rsa_sign(method: str, path: str) -> dict:
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
            raw_key = open(_key_file).read().replace('\\n', '\n')
            pem_bytes = raw_key.encode()
        else:
            raw = CFG.API_SECRET
            # Render stores env vars with literal 
 - convert to real newlines
            raw = raw.replace('\n', '
')
            if '
' not in raw and 'BEGIN' in raw:
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
    return headers
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import os as _os
        _key_file = "/etc/secrets/kalshi_key.pem"
        if _os.path.exists(_key_file):
            raw_key = open(_key_file).read().replace('\\n', '\n')
            pem_bytes = raw_key.encode()
        else:
            raw = CFG.API_SECRET
            # Render stores env vars with literal 
 - convert to real newlines
            raw = raw.replace('\n', '
')
            if '
' not in raw and 'BEGIN' in raw:
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
    return headers

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend

        # Try secret file first, fall back to env var
        import os as _os
        _key_file = "/etc/secrets/kalshi_key.pem"
        if _os.path.exists(_key_file):
            raw_key = open(_key_file).read().replace('\\n', '\n')
            pem_bytes = raw_key.encode()
        else:
            raw = CFG.API_SECRET.replace("\n", "
")
            pem_bytes = raw.encode()
        private_key = serialization.load_pem_private_key(
            pem_bytes, password=None, backend=default_backend()
        )
        signature = private_key.sign(
            msg.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=32
            ),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(signature).decode()

        headers.update({
            "KALSHI-ACCESS-KEY":       CFG.API_KEY,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        })
    except Exception as e:
        log.warning("RSA signing failed: %s", e)

    return headers


def _public_headers() -> dict:
    """Headers for unauthenticated public Kalshi reads (markets endpoint)."""
    return {"Content-Type": "application/json", "Accept": "application/json"}


# -----------------------------------------------------------------------------
# DATA MODELS
# -----------------------------------------------------------------------------

@dataclass
class Signal:
    """Mirrors what the scanner returns. We just receive these."""
    ticker:    str
    title:     str
    direction: str
    price:     float
    composite: int
    ofi:       int
    days:      float
    edge:      float
    # v2.0: liquidity metadata for routing decisions
    volume:        float = 0.0  # lifetime contracts traded
    volume_24h:    float = 0.0  # last 24hr contracts traded
    open_interest: float = 0.0
    # Routing assignment (set by route_signal())
    path:       str = "SKIP"    # "LIQUID" | "THIN" | "SKIP"
    skip_reason: str = ""
    kelly_size: float = 0.0
    ts:        float = field(default_factory=time.time)


@dataclass
class Position:
    ticker:      str
    title:       str
    direction:   str
    entry_price: float
    size_usd:    float
    contracts:   int
    order_id:    str
    # v2.0: track path + entry-time volume snapshot for velocity decay
    path:        str = "LIQUID"  # "LIQUID" | "THIN"
    entry_volume_24h: float = 0.0
    opened_at:   float = field(default_factory=time.time)
    closed_at:   Optional[float] = None
    exit_price:  Optional[float] = None
    pnl:         float = 0.0
    status:      str = "open"


# -----------------------------------------------------------------------------
# SCANNER CLIENT — pulls qualified signals from kalshi_server's /api/signals
# -----------------------------------------------------------------------------

def poll_scanner_signals() -> tuple[list[Signal], dict]:
    """
    Pull qualified signals from the scanner. Returns (signals, raw_response).
    Empty list on any failure — fail closed.
    """
    url = CFG.SCANNER_URL.rstrip("/") + "/api/signals"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.error("Scanner HTTP %s: %s", e.code, e.read()[:200])
        return [], {}
    except Exception as e:
        log.error("Scanner fetch failed: %s", e)
        return [], {}

    raw_signals = data.get("signals", [])
    signals = []
    for s in raw_signals:
        f = s.get("factors", {})
        signals.append(Signal(
            ticker    = s["ticker"],
            title     = s.get("title", s["ticker"]),
            direction = s["direction"],
            price     = float(s["price"]),
            composite = int(f.get("composite", 0)),
            ofi       = int(f.get("ofi", 0)),
            days      = float(f.get("days", s.get("days_to_close", 0))),
            edge      = float(f.get("edge", 0)),
            # v2.0: liquidity metadata from scanner
            volume        = float(s.get("volume", 0)),
            volume_24h    = float(s.get("volume_24h", 0)),
            open_interest = float(s.get("open_interest", 0)),
        ))

    return signals, data


# -----------------------------------------------------------------------------
# KELLY SIZER — kept for revert path, NOT USED in v2.3 measurement mode
# -----------------------------------------------------------------------------

def kelly_size(edge: float, price: float, capital: float, open_positions: int) -> float:
    """
    v2.3: NOT CALLED in measurement mode. Kept here so we can revert to
    score-driven sizing once the scoring system has been validated.
    """
    if edge <= 0 or price <= 0 or price >= 1:
        return 0.0
    p_win  = min(0.95, price + edge)
    p_lose = 1 - p_win
    b = (1.0 - price) / price
    raw_kelly = (b * p_win - p_lose) / b
    if raw_kelly <= 0:
        return 0.0
    available = capital * (1 - open_positions / max(CFG.MAX_OPEN, 1))
    size = raw_kelly * CFG.KELLY_FRACTION * available
    return max(CFG.MIN_POSITION_USD, min(CFG.MAX_POSITION_USD, size))


# -----------------------------------------------------------------------------
# v2.0 LIQUIDITY ROUTING — the overlay layer
# -----------------------------------------------------------------------------

def route_signal(sig: Signal, thin_book_used_usd: float, total_capital: float) -> Signal:
    """
    Assign sig.path = "LIQUID" | "THIN" | "SKIP" and set skip_reason if applicable.

    v2.3: gate values loosened (vol>=2K, v24h>=100, TTR<=14d). Logic unchanged.
    """
    failures = []

    # ----- Gate 1: TTR (universal) -----
    ttr_pass = sig.days <= CFG.MAX_TTR_DAYS
    if not ttr_pass:
        failures.append(f"ttr_{sig.days:.1f}d_gt_{CFG.MAX_TTR_DAYS:.0f}d")

    # ----- Gate 2: LIQUID-path candidate? -----
    is_liquid_candidate = (sig.volume >= CFG.LIQUID_VOLUME_MIN
                           and sig.volume_24h >= CFG.LIQUID_VOLUME_24H_MIN)

    # ----- Gate 3: THIN-path candidate? -----
    is_thin_by_volume = sig.volume < CFG.LIQUID_VOLUME_MIN
    thin_cap_usd = total_capital * CFG.THIN_CAPITAL_PCT
    thin_book_has_room = thin_book_used_usd < thin_cap_usd

    # ----- Decision tree -----

    if not is_liquid_candidate:
        if sig.volume < CFG.LIQUID_VOLUME_MIN:
            # Below liquid volume threshold — thin-path candidate by volume
            if not thin_book_has_room:
                failures.append(
                    f"thin_book_full_{thin_book_used_usd:.0f}_of_{thin_cap_usd:.0f}"
                )
        else:
            # Above liquid volume but below freshness — stale-but-deep
            failures.append(
                f"vol24h_{sig.volume_24h:.0f}_below_{CFG.LIQUID_VOLUME_24H_MIN:.0f}"
            )

    # ----- Assign final path -----
    if failures:
        sig.path = "SKIP"
        sig.skip_reason = ",".join(failures)
        return sig

    if is_liquid_candidate:
        sig.path = "LIQUID"
    else:
        sig.path = "THIN"
    sig.skip_reason = ""
    return sig


def size_thin_position(sig: Signal, capital: float, open_positions: int) -> float:
    """
    v2.3: NOT CALLED in measurement mode. Kept here for revert path.
    Sizing for THIN path: min(half-Kelly, 5% × contract_volume × price).
    """
    kelly = kelly_size(sig.edge, sig.price, capital, open_positions)
    volume_cap_usd = sig.volume * CFG.THIN_SIZE_PCT_OF_VOLUME * sig.price
    sized = max(1.0, min(kelly, volume_cap_usd))
    return min(sized, CFG.MAX_POSITION_USD)


# -----------------------------------------------------------------------------
# KALSHI MARKET CHECKS — PUBLIC endpoints, no auth required (v1.9)
# -----------------------------------------------------------------------------

def fetch_market_volume_24h(ticker: str) -> Optional[float]:
    """
    v2.0: Fetch current 24-hour volume for a market. PUBLIC endpoint.
    Used by LIQUID positions to detect velocity decay (flow walked away).
    Returns volume_24h as float, or None on failure.
    """
    url = f"{CFG.KALSHI_BASE}/markets/{ticker}"
    try:
        req = urllib.request.Request(url, headers=_public_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        m = data.get("market", data)
        v24 = m.get("volume_24h")
        if v24 is None:
            return None
        return float(v24)
    except Exception as e:
        log.debug("fetch_market_volume_24h %s: %s", ticker, e)
        return None


def fetch_market_price(ticker: str) -> Optional[float]:
    """
    Fetch current market price for a ticker. PUBLIC endpoint — no auth needed.
    Returns price as decimal (0.0-1.0) or None on failure.
    """
    url = f"{CFG.KALSHI_BASE}/markets/{ticker}"
    try:
        req = urllib.request.Request(url, headers=_public_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        m  = data.get("market", data)
        ra = m.get("yes_ask")
        if ra is None:
            ra = m.get("last_price")
        if ra is None:
            return None
        return ra / 100.0 if ra > 1 else float(ra)
    except urllib.error.HTTPError as e:
        log.debug("fetch_market_price %s: HTTP %s", ticker, e.code)
        return None
    except Exception as e:
        log.debug("fetch_market_price %s: %s", ticker, e)
        return None


def fetch_market_status(ticker: str) -> Optional[dict]:
    """
    Verify a market's actual status from Kalshi. PUBLIC endpoint.
    Returns dict with is_open / status / last_price, or None on transient failure.
    """
    url = f"{CFG.KALSHI_BASE}/markets/{ticker}"
    try:
        req = urllib.request.Request(url, headers=_public_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        m = data.get("market", data)
        status = (m.get("status") or "").lower()
        last   = m.get("last_price") or m.get("yes_ask")
        return {
            "is_open":    status in ("open", "active", "trading"),
            "status":     status,
            "last_price": (float(last) / 100.0 if last and float(last) > 1
                           else (float(last) if last is not None else None)),
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"is_open": False, "status": "not_found", "last_price": None}
        log.warning("Status check failed for %s: HTTP %s", ticker, e.code)
        return None
    except Exception as e:
        log.warning("Status check error for %s: %s", ticker, e)
        return None


def batch_fetch_open_prices(open_tickers: list[str]) -> dict[str, float]:
    """
    Fetch current price for every open-position ticker.
    Called at the top of each cycle BEFORE check_exits.
    """
    out: dict[str, float] = {}
    if not open_tickers:
        return out

    fetched = 0
    missed  = 0
    for ticker in open_tickers:
        px = fetch_market_price(ticker)
        if px is not None:
            out[ticker] = px
            fetched += 1
        else:
            missed += 1

    if missed:
        log.info("Mark-to-market: %d/%d open prices fetched (%d failed)",
                 fetched, len(open_tickers), missed)
    else:
        log.info("Mark-to-market: %d/%d open prices fetched",
                 fetched, len(open_tickers))
    return out


# -----------------------------------------------------------------------------
# ORDER EXECUTION
# -----------------------------------------------------------------------------

def place_order(ticker: str, side: str, contracts: int, price_cents: int) -> dict:
    if CFG.PAPER_TRADE:
        oid = f"PAPER-{ticker}-{side.upper()}-{int(time.time())}"
        log.info("[PAPER] %s %s %d contracts @ %dc  ->  %s",
                 ticker, side.upper(), contracts, price_cents, oid)
        return {"order_id": oid, "status": "paper_filled", "paper": True}

    path      = "/trade-api/v2/portfolio/orders"
    price_key = "yes_price" if side == "yes" else "no_price"
    body      = json.dumps({
        "ticker": ticker, "side": side, "type": "limit",
        "action": "buy", "count": contracts, price_key: price_cents,
    }).encode()
    headers = _rsa_sign("POST", path)
    req = urllib.request.Request(
        CFG.KALSHI_BASE + "/portfolio/orders", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.error("Order rejected HTTP %s: %s", e.code, e.read())
        return {}
    except Exception as e:
        log.error("Order error: %s", e)
        return {}


# -----------------------------------------------------------------------------
# TRADE JOURNAL
# -----------------------------------------------------------------------------

def log_trade(event: str, data: dict):
    record = {"ts": datetime.now().isoformat(), "event": event, **data}
    with open(CFG.TRADE_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


# -----------------------------------------------------------------------------
# RISK MANAGER
# -----------------------------------------------------------------------------

class RiskManager:
    def __init__(self):
        self.daily_pnl  = 0.0
        self.open:  dict[str, Position] = {}
        self.closed: list[Position]     = []
        self.last_marks: dict[str, float] = {}
        self.last_vol24h: dict[str, float] = {}
        self.skip_counts: dict[str, int] = {}

    def thin_book_used_usd(self) -> float:
        return sum(p.size_usd for p in self.open.values() if p.path == "THIN")

    def liquid_book_used_usd(self) -> float:
        return sum(p.size_usd for p in self.open.values() if p.path == "LIQUID")

    def can_trade(self, sig: Signal) -> tuple[bool, str]:
        if -self.daily_pnl >= CFG.MAX_DAILY_LOSS:
            return False, f"Circuit breaker: daily loss ${-self.daily_pnl:.2f}"
        if len(self.open) >= CFG.MAX_OPEN:
            return False, f"Max open positions ({CFG.MAX_OPEN}) reached"
        if sig.ticker in self.open:
            return False, f"Already have position in {sig.ticker}"
        # v2.3: with FLAT_POSITION_USD=$25, this check is effectively dead
        # but kept as a safety net in case someone reverts FLAT and forgets
        # to restore Kelly. Min is $5 (PAPER) so $25 always passes.
        if sig.kelly_size < CFG.MIN_POSITION_USD:
            return False, f"Position size ${sig.kelly_size:.2f} below minimum"
        return True, "ok"

    def record_skip(self, reason: str):
        """v2.1: tally skip reasons for /state visibility."""
        if not reason:
            return
        for r in reason.split(","):
            r = r.strip()
            if not r:
                continue
            bucket = self._bucket_skip_reason(r)
            self.skip_counts[bucket] = self.skip_counts.get(bucket, 0) + 1

    @staticmethod
    def _bucket_skip_reason(reason: str) -> str:
        """Map a specific skip reason to a generic bucket for tallying."""
        if reason.startswith("ttr_"):
            return "ttr_gt_max"
        if reason.startswith("vol24h_"):
            return "vol24h_below_min"
        if reason.startswith("thin_book_full"):
            return "thin_book_full"
        if reason == "thin_path_disabled_for_experiment":
            return "thin_path_disabled_for_experiment"
        if "max open" in reason.lower() or "max_open" in reason.lower():
            return "max_open_reached"
        if "circuit" in reason.lower() or "daily loss" in reason.lower():
            return "circuit_breaker"
        if "already have" in reason.lower():
            return "duplicate_ticker"
        if "below minimum" in reason.lower():
            return "below_min_position"
        return reason

    def open_position(self, pos: Position):
        self.open[pos.ticker] = pos
        log.info("Opened: %s %s $%.2f (%d contracts)",
                 pos.ticker, pos.direction, pos.size_usd, pos.contracts)

    def close_position(self, ticker: str, exit_price: float, reason: str = "manual"):
        if ticker not in self.open:
            return
        pos = self.open.pop(ticker)
        pos.closed_at  = time.time()
        pos.exit_price = exit_price
        pos.status     = "closed"

        if pos.direction == "LONG":
            pos.pnl = (exit_price - pos.entry_price) * pos.contracts
        else:
            pos.pnl = (pos.entry_price - exit_price) * pos.contracts

        self.daily_pnl += pos.pnl
        self.closed.append(pos)
        self.last_marks.pop(ticker, None)
        log_trade("close", {
            "ticker": ticker, "direction": pos.direction,
            "entry": pos.entry_price, "exit": exit_price,
            "contracts": pos.contracts, "pnl": pos.pnl, "reason": reason,
        })
        log.info("Closed %s  reason=%s  P&L: $%.2f  Daily: $%.2f",
                 ticker, reason, pos.pnl, self.daily_pnl)

    def sweep_orphans(self, scanner_tickers: set, price_map: dict):
        now = time.time()
        to_close = []
        for ticker, pos in self.open.items():
            age_hours = (now - pos.opened_at) / 3600

            if ticker in scanner_tickers:
                if age_hours > CFG.ORPHAN_TIMEOUT_HOURS:
                    exit_px = price_map.get(ticker, pos.entry_price)
                    to_close.append((ticker, exit_px, "orphan_timeout"))
                    log.info("ORPHAN TIMEOUT: %s open %.1fh -> closing", ticker, age_hours)
                continue

            status = fetch_market_status(ticker)

            if status is None:
                log.debug("Skipping orphan check for %s — status check failed", ticker)
                continue

            if status["is_open"]:
                log.debug("%s not in scanner but still open on Kalshi — keeping", ticker)
                if age_hours > CFG.ORPHAN_TIMEOUT_HOURS:
                    exit_px = status["last_price"] or pos.entry_price
                    to_close.append((ticker, exit_px, "orphan_timeout"))
                    log.info("ORPHAN TIMEOUT: %s open %.1fh -> closing", ticker, age_hours)
                continue

            exit_px = status["last_price"] if status["last_price"] is not None else pos.entry_price
            to_close.append((ticker, exit_px, f"market_{status['status']}"))
            log.info("CONFIRMED CLOSED: %s status=%s (age %.1fh) -> closing at %.2f",
                     ticker, status["status"], age_hours, exit_px)

        for ticker, price, reason in to_close:
            self.close_position(ticker, price, reason)

    def check_exits(self, price_map: dict, vol24h_map: dict = None):
        """
        v2.0: Path-aware exits.

        LIQUID positions:
          - Profit target (+20%) / Stop loss (-10%)
          - Velocity decay (current_vol24h < 50% of entry vol24h),
            but only after 30-min grace period

        THIN positions:
          - No exits. Hold to settlement. (In v2.3 with ENABLE_THIN_EXECUTION
            =False this code path is effectively unreachable but kept intact.)
        """
        if vol24h_map is None:
            vol24h_map = {}

        to_close = []
        checked  = 0
        skipped  = 0
        thin_held = 0

        for ticker, pos in self.open.items():
            curr = price_map.get(ticker)
            if curr is None:
                curr = fetch_market_price(ticker)
                if curr is None:
                    skipped += 1
                    continue
                price_map[ticker] = curr
            checked += 1
            self.last_marks[ticker] = curr

            if pos.path == "THIN":
                thin_held += 1
                continue

            pnl_pct = ((curr - pos.entry_price) / pos.entry_price
                       if pos.direction == "LONG"
                       else (pos.entry_price - curr) / pos.entry_price)

            if pnl_pct >= CFG.PROFIT_TARGET:
                log.info("PROFIT TARGET hit on %s (+%.0f%%)  entry=%.2f curr=%.2f",
                         ticker, pnl_pct*100, pos.entry_price, curr)
                to_close.append((ticker, curr, "profit_target"))
                continue

            if pnl_pct <= -CFG.STOP_LOSS:
                log.info("STOP LOSS hit on %s (%.0f%%)  entry=%.2f curr=%.2f",
                         ticker, pnl_pct*100, pos.entry_price, curr)
                to_close.append((ticker, curr, "stop_loss"))
                continue

            age_sec = time.time() - pos.opened_at
            if age_sec < CFG.VELOCITY_DECAY_GRACE_S:
                continue

            curr_v24h = vol24h_map.get(ticker)
            if curr_v24h is None:
                continue

            self.last_vol24h[ticker] = curr_v24h
            if pos.entry_volume_24h > 0:
                decay_ratio = curr_v24h / pos.entry_volume_24h
                if decay_ratio < CFG.VELOCITY_DECAY_THRESHOLD:
                    log.info("VELOCITY DECAY on %s: v24h %.0f -> %.0f (%.0f%% of entry) age=%.0fmin",
                             ticker, pos.entry_volume_24h, curr_v24h,
                             decay_ratio * 100, age_sec / 60)
                    to_close.append((ticker, curr, "velocity_decay"))

        log.info("Exit check: %d marked (%d thin held), %d skipped, %d exits firing",
                 checked, thin_held, skipped, len(to_close))

        for ticker, price, reason in to_close:
            self.close_position(ticker, price, reason)
            log_trade("exit", {"ticker": ticker, "reason": reason, "exit_price": price})

    def summary(self) -> str:
        wins  = [p for p in self.closed if p.pnl > 0]
        total = len(self.closed)
        wr    = len(wins)/total*100 if total else 0
        return (f"Open: {len(self.open)} | Closed: {total} | "
                f"Win rate: {wr:.0f}% | Daily P&L: ${self.daily_pnl:.2f}")


# -----------------------------------------------------------------------------
# MAIN LOOP
# -----------------------------------------------------------------------------

def print_trade(sig: Signal, position: Position):
    arrow = "^ LONG" if sig.direction == "LONG" else "v SHORT"
    mode  = "PAPER" if CFG.PAPER_TRADE else "LIVE"
    path_tag = f"[{position.path}]"
    extras = ""
    if position.path == "LIQUID":
        extras = f"  v24h:{sig.volume_24h:.0f}"
    elif position.path == "THIN":
        extras = f"  vol:{sig.volume:.0f} (hold to resolve)"
    print(f"""
  +-----------------------------------------------------+
  |  {mode} TRADE  {arrow}  {path_tag}
  |  {sig.title[:52]}
  |  Ticker  : {sig.ticker}
  |  Price   : {sig.price*100:.1f}c   Contracts: {position.contracts}
  |  Size    : ${position.size_usd:.2f}{extras}
  |  Score   : {sig.composite}  OFI:{sig.ofi}  TTR:{sig.days:.1f}d
  |  Order   : {position.order_id}
  +-----------------------------------------------------+""")


def run():
    try:
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        import subprocess
        log.info("Installing cryptography package...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "cryptography", "--break-system-packages", "-q"])

    mode = "PAPER TRADING" if CFG.PAPER_TRADE else "*** LIVE TRADING ***"
    thin_label = "ENABLED" if ENABLE_THIN_EXECUTION else "DISABLED (measurement mode)"
    print(f"""
+======================================================+
|   KALSHI EXECUTOR v2.3 — WIDE-FIRST MODE             |
|   Mode      : {mode:<39}|
|   Sizing    : FLAT ${FLAT_POSITION_USD:.0f} per trade                       |
|   THIN exec : {thin_label:<39}|
|   Capital   : ${CFG.TOTAL_CAPITAL:>9,.0f}                              |
|   Auth      : {'RSA KEY SET' if CFG.API_KEY else 'NO API KEY':<39}|
|   Scanner   : {CFG.SCANNER_URL:<39}|
|   Max open  : {CFG.MAX_OPEN:<39}|
|   Universal : TTR <= {CFG.MAX_TTR_DAYS:.0f} days                           |
|   LIQUID    : vol>={CFG.LIQUID_VOLUME_MIN:.0f}, v24h>={CFG.LIQUID_VOLUME_24H_MIN:.0f}, +{int(CFG.PROFIT_TARGET*100)}%/-{int(CFG.STOP_LOSS*100)}% exits   |
|   Velocity  : exit if v24h < {int(CFG.VELOCITY_DECAY_THRESHOLD*100)}% of entry (after {int(CFG.VELOCITY_DECAY_GRACE_S/60)}min) |
|   Breaker   : ${CFG.MAX_DAILY_LOSS:.0f} daily loss                       |
+======================================================+
""")

    risk    = RiskManager()
    cycle   = 0
    capital = CFG.TOTAL_CAPITAL

    while True:
        cycle += 1
        try:
            _shared["cycle"] = cycle
        except NameError:
            pass

        log.info("-- Cycle %d  %s ------------------------------",
                 cycle, datetime.now().strftime("%H:%M:%S"))

        signals, raw = poll_scanner_signals()
        if not signals and not raw:
            log.warning("Scanner unreachable or returned no data - retrying in %ds",
                        CFG.SCAN_INTERVAL)
            time.sleep(CFG.SCAN_INTERVAL)
            continue

        scanned   = raw.get("scanned", 0)
        qualified = raw.get("qualified", len(signals))
        log.info("Scanner returned %d signals (from %d scanned) | %s",
                 qualified, scanned, risk.summary())

        # ---- Build price_map (mark-to-market source) ----
        price_map = {s.ticker: s.price for s in signals}
        scanner_tickers = set(price_map.keys())

        open_tickers_needing_mark = [t for t in risk.open.keys() if t not in price_map]
        if open_tickers_needing_mark:
            mtm_prices = batch_fetch_open_prices(open_tickers_needing_mark)
            price_map.update(mtm_prices)

        # ---- Fetch current volume_24h for LIQUID open positions ----
        vol24h_map: dict[str, float] = {}
        now_t = time.time()
        for ticker, pos in risk.open.items():
            if pos.path != "LIQUID":
                continue
            if now_t - pos.opened_at < CFG.VELOCITY_DECAY_GRACE_S:
                continue
            v24 = fetch_market_volume_24h(ticker)
            if v24 is not None:
                vol24h_map[ticker] = v24

        risk.sweep_orphans(scanner_tickers, price_map)
        risk.check_exits(price_map, vol24h_map)

        # ---- v2.3: Route signals; only LIQUID enters execution ----
        liquid_signals = []
        thin_signals_routed = 0  # counted but not executed
        skip_count_this_cycle = 0
        for s in signals:
            route_signal(
                s,
                risk.thin_book_used_usd(),
                capital + risk.liquid_book_used_usd() + risk.thin_book_used_usd(),
            )
            if s.path == "LIQUID":
                # v2.3: flat sizing
                s.kelly_size = FLAT_POSITION_USD
                liquid_signals.append(s)
            elif s.path == "THIN":
                thin_signals_routed += 1
                if ENABLE_THIN_EXECUTION:
                    # Path kept intact for revert; not reached when False
                    s.kelly_size = FLAT_POSITION_USD
                    liquid_signals.append(s)  # treated same downstream
                else:
                    risk.record_skip("thin_path_disabled_for_experiment")
            else:
                risk.record_skip(s.skip_reason)
                skip_count_this_cycle += 1

        log.info("Routing this cycle: %d LIQUID, %d THIN routed (%s), %d SKIP",
                 len(liquid_signals), thin_signals_routed,
                 "executed" if ENABLE_THIN_EXECUTION else "blocked",
                 skip_count_this_cycle)

        sized_signals = liquid_signals  # v2.3: LIQUID-only execution

        if sized_signals:
            log.info("Top %d signals:", min(5, len(sized_signals)))
            for s in sized_signals[:5]:
                log.info("  [%-6s] %s  %s  score=%d  OFI=%d  TTR=%.1fd  size=$%.0f",
                         s.path, s.ticker, s.direction, s.composite, s.ofi,
                         s.days, s.kelly_size)
        else:
            log.info("No LIQUID signals this cycle (THIN routed: %d)", thin_signals_routed)

        trades_this_cycle = 0
        for sig in sized_signals:
            ok, reason = risk.can_trade(sig)
            if not ok:
                log.info("Skipping %s: %s", sig.ticker, reason)
                risk.record_skip(reason)
                continue

            side        = "yes" if sig.direction == "LONG" else "no"
            price_cents = int(sig.price * 100)
            # v2.3: $25 / price = contracts. Cap at 1 minimum.
            contracts   = max(1, int(sig.kelly_size / max(sig.price, 0.01)))

            resp = place_order(sig.ticker, side, contracts, price_cents)
            if resp.get("order_id"):
                pos = Position(
                    ticker=sig.ticker, title=sig.title,
                    direction=sig.direction, entry_price=sig.price,
                    size_usd=sig.kelly_size, contracts=contracts,
                    order_id=resp["order_id"],
                    path=sig.path,
                    entry_volume_24h=sig.volume_24h,
                )
                risk.open_position(pos)
                print_trade(sig, pos)
                log_trade("open", {
                    "ticker": sig.ticker, "direction": sig.direction,
                    "price": sig.price, "contracts": contracts,
                    "size_usd": sig.kelly_size, "composite": sig.composite,
                    "ofi": sig.ofi, "ttr_days": sig.days,
                    "path": sig.path,
                    "volume": sig.volume,
                    "volume_24h": sig.volume_24h,
                    "order_id": resp["order_id"], "paper": CFG.PAPER_TRADE,
                })
                trades_this_cycle += 1
                capital -= sig.kelly_size
            else:
                log.warning("Order failed for %s", sig.ticker)

        if trades_this_cycle == 0 and sized_signals:
            log.info("Signals routed but none executed (risk gates or order failures)")

        if cycle % 5 == 0 or trades_this_cycle > 0:
            print(f"\n  -- Portfolio @ {datetime.now().strftime('%H:%M:%S')} --")
            print(f"  {risk.summary()}")
            if risk.open:
                print(f"  Open positions ({len(risk.open)}):")
                for t, p in list(risk.open.items())[:20]:
                    curr = price_map.get(t, p.entry_price)
                    upnl = (curr - p.entry_price) * p.contracts if p.direction == "LONG" \
                           else (p.entry_price - curr) * p.contracts
                    print(f"    {t:<45} {p.direction:<6} entry={p.entry_price*100:.1f}c  "
                          f"curr={curr*100:.1f}c  uPnL=${upnl:+.2f}")
                if len(risk.open) > 20:
                    print(f"    ... and {len(risk.open) - 20} more")
            print()

        time.sleep(CFG.SCAN_INTERVAL)


# -----------------------------------------------------------------------------
# HEALTH SERVER (for Render — exposes /state, /api/trades, /api/trades/raw)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import threading
    import http.server as _hs

    _shared = {"risk": None, "cycle": 0, "started_at": time.time()}

    class HealthHandler(_hs.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            return

        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path == "/api/trades" or self.path.startswith("/api/trades?"):
                    trades = []
                    try:
                        with open(CFG.TRADE_LOG, "r") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    trades.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                    except FileNotFoundError:
                        pass

                    by_ticker_opens = {}
                    round_trips = []
                    for r in trades:
                        ev = r.get("event")
                        ticker = r.get("ticker", "")
                        if ev == "open":
                            by_ticker_opens.setdefault(ticker, []).append(r)
                        elif ev == "close":
                            opens = by_ticker_opens.get(ticker, [])
                            if opens:
                                o = opens.pop(0)
                                round_trips.append({
                                    "ticker":    ticker,
                                    "direction": o.get("direction"),
                                    "opened_at": o.get("ts"),
                                    "closed_at": r.get("ts"),
                                    "entry":     o.get("price"),
                                    "exit":      r.get("exit"),
                                    "contracts": o.get("contracts"),
                                    "size_usd":  o.get("size_usd"),
                                    "composite": o.get("composite"),
                                    "ofi":       o.get("ofi"),
                                    "ttr_days":  o.get("ttr_days"),
                                    "pnl":       r.get("pnl"),
                                    "reason":    r.get("reason"),
                                    "paper":     o.get("paper"),
                                })

                    closed = round_trips
                    open_trades = [
                        {"ticker": t, "opens": opens}
                        for t, opens in by_ticker_opens.items() if opens
                    ]
                    total_pnl = sum((rt.get("pnl") or 0) for rt in closed)
                    wins   = [rt for rt in closed if (rt.get("pnl") or 0) > 0]
                    losses = [rt for rt in closed if (rt.get("pnl") or 0) < 0]
                    flats  = [rt for rt in closed if (rt.get("pnl") or 0) == 0]

                    payload = {
                        "version":      "v2.3",
                        "total_events": len(trades),
                        "round_trips":  len(closed),
                        "still_open":   sum(len(o["opens"]) for o in open_trades),
                        "summary": {
                            "wins":          len(wins),
                            "losses":        len(losses),
                            "flat":          len(flats),
                            "win_rate_pct":  (len(wins) / len(closed) * 100) if closed else 0,
                            "total_pnl":     total_pnl,
                            "avg_pnl":       (total_pnl / len(closed)) if closed else 0,
                            "best_trade":    max((rt.get("pnl") or 0) for rt in closed) if closed else 0,
                            "worst_trade":   min((rt.get("pnl") or 0) for rt in closed) if closed else 0,
                        },
                        "trades": closed,
                    }
                    return self._send_json(payload)

                if self.path == "/api/trades/raw":
                    trades = []
                    try:
                        with open(CFG.TRADE_LOG, "r") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    trades.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                    except FileNotFoundError:
                        pass
                    return self._send_json({"version": "v2.3", "events": trades})

                if self.path == "/state":
                    risk = _shared.get("risk")
                    open_positions_payload = []
                    if risk:
                        for p in risk.open.values():
                            curr = risk.last_marks.get(p.ticker)
                            if curr is not None:
                                if p.direction == "LONG":
                                    upnl = (curr - p.entry_price) * p.contracts
                                else:
                                    upnl = (p.entry_price - curr) * p.contracts
                            else:
                                upnl = 0.0
                            open_positions_payload.append({
                                "ticker":        p.ticker,
                                "direction":     p.direction,
                                "entry_price":   p.entry_price,
                                "current_price": curr,
                                "contracts":     p.contracts,
                                "size_usd":      p.size_usd,
                                "upnl":          upnl,
                                "pnl":           p.pnl,
                                "path":              p.path,
                                "entry_volume_24h":  p.entry_volume_24h,
                                "current_volume_24h": risk.last_vol24h.get(p.ticker),
                            })

                    thin_used = risk.thin_book_used_usd() if risk else 0.0
                    liquid_used = risk.liquid_book_used_usd() if risk else 0.0
                    thin_cap = CFG.TOTAL_CAPITAL * CFG.THIN_CAPITAL_PCT

                    payload = {
                        "version":      "v2.3",
                        "mode":         "wide-first measurement (flat $25, LIQUID only, TTR<=14d, 10%/5% exits)",
                        "cycle":        _shared.get("cycle", 0),
                        "uptime_sec":   int(time.time() - _shared["started_at"]),
                        "open_count":   len(risk.open) if risk else 0,
                        "closed_count": len(risk.closed) if risk else 0,
                        "daily_pnl":    risk.daily_pnl if risk else 0,
                        "scanner_url":  CFG.SCANNER_URL,
                        "thin_book_used_usd":   thin_used,
                        "thin_book_cap_usd":    thin_cap,
                        "thin_book_pct":        (thin_used / thin_cap * 100) if thin_cap > 0 else 0,
                        "liquid_book_used_usd": liquid_used,
                        "skip_counts": dict(risk.skip_counts) if risk else {},
                        "config": {
                            "flat_position_usd":     FLAT_POSITION_USD,
                            "enable_thin_execution": ENABLE_THIN_EXECUTION,
                            "profit_target":    CFG.PROFIT_TARGET,
                            "stop_loss":        CFG.STOP_LOSS,
                            "kelly_fraction":   CFG.KELLY_FRACTION,
                            "max_open":         CFG.MAX_OPEN,
                            "min_position_usd": CFG.MIN_POSITION_USD,
                            "max_position_usd": CFG.MAX_POSITION_USD,
                            "total_capital":    CFG.TOTAL_CAPITAL,
                            "max_daily_loss":   CFG.MAX_DAILY_LOSS,
                            "max_ttr_days":              CFG.MAX_TTR_DAYS,
                            "liquid_volume_min":         CFG.LIQUID_VOLUME_MIN,
                            "liquid_volume_24h_min":     CFG.LIQUID_VOLUME_24H_MIN,
                            "velocity_decay_grace_s":    CFG.VELOCITY_DECAY_GRACE_S,
                            "velocity_decay_threshold":  CFG.VELOCITY_DECAY_THRESHOLD,
                            "thin_capital_pct":          CFG.THIN_CAPITAL_PCT,
                            "thin_size_pct_of_volume":   CFG.THIN_SIZE_PCT_OF_VOLUME,
                        },
                        "open_positions": open_positions_payload,
                    }
                    return self._send_json(payload)

                payload = {
                    "status":     "running",
                    "version":    "v2.3",
                    "mode_note":  "measurement mode: flat $%.0f sizing, LIQUID-only execution" % FLAT_POSITION_USD,
                    "cycle":      _shared.get("cycle", 0),
                    "trade_mode": "PAPER" if CFG.PAPER_TRADE else "LIVE",
                    "architecture": "executor with liquidity routing overlay",
                    "scanner_url": CFG.SCANNER_URL,
                    "endpoints": ["/", "/state", "/api/trades", "/api/trades/raw"],
                }
                return self._send_json(payload)

            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    def _start_health_server():
        port = int(os.environ.get("PORT", "10000"))
        try:
            server = _hs.HTTPServer(("0.0.0.0", port), HealthHandler)
            log.info("Health server listening on port %d", port)
            server.serve_forever()
        except Exception as e:
            log.error("Health server failed: %s", e)

    _health_thread = threading.Thread(target=_start_health_server, daemon=True)
    _health_thread.start()

    _orig_rm_init = RiskManager.__init__
    def _rm_init_with_register(self):
        _orig_rm_init(self)
        _shared["risk"] = self
    RiskManager.__init__ = _rm_init_with_register

    try:
        run()
    except KeyboardInterrupt:
        print("\n\nTrader stopped.")
