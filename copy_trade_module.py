"""
Kalshi Copy Trade Module — v2.0
================================
Option A: Live leaderboard ranking — scrapes all traders, ranks by win rate,
          follows top performers dynamically. No fixed % floor — best always wins.
Option B: Informed flow detection — order book imbalance signals.

Copy trades logged separately at /data/copy_trades.json
Hard cap: $12.50 per copy trade always.
"""

import json
import logging
import time
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict

logger = logging.getLogger("copy_trader")

# =============================================================================
# PARAMETERS
# =============================================================================

COPY_FLAT_USD           = 12.50   # $12.50 per copy trade — hard cap, no exceptions
COPY_TOP_N              = 5       # follow top N traders by win rate
COPY_MIN_TRADES         = 20      # minimum trades to be ranked
COPY_SCAN_INTERVAL_S    = 300     # re-rank leaderboard every 5 minutes
FLOW_SCAN_INTERVAL_S    = 60      # scan order books every 60 seconds
FLOW_IMBALANCE_THRESH   = 3.0     # buy/sell ratio must exceed this
FLOW_MIN_VOLUME         = 500     # minimum order book volume
COPY_PRICE_MIN          = 0.30    # min contract price for copy trades
COPY_PRICE_MAX          = 0.85    # max contract price for copy trades
COPY_LOG_FILE           = "/data/copy_trades.json"
LEADERBOARD_URL         = "https://predicting.top/api/traders?platform=kalshi&sort=winrate&limit=100"

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class RankedTrader:
    rank:         int
    username:     str
    win_rate:     float
    total_trades: int
    total_pnl:    float
    win_count:    int
    loss_count:   int
    consistency:  float   # win_rate weighted by trade count
    last_updated: str = ""

@dataclass
class FlowSignal:
    ticker:    str
    direction: str
    imbalance: float
    yes_vol:   int
    no_vol:    int
    yes_bid:   float
    timestamp: str

@dataclass
class CopyTrade:
    source:    str        # "SOCIAL" or "FLOW"
    ticker:    str
    direction: str
    contracts: int
    cost:      float
    reason:    str
    rank:      Optional[int]
    trader:    Optional[str]
    win_rate:  Optional[float]
    imbalance: Optional[float]
    timestamp: str
    status:    str = "LOGGED"

# =============================================================================
# COPY TRADE LOGGER
# =============================================================================

def log_copy_trade(trade: CopyTrade):
    try:
        rec = {
            "ts": trade.timestamp, "source": trade.source,
            "ticker": trade.ticker, "direction": trade.direction,
            "contracts": trade.contracts, "cost": trade.cost,
            "reason": trade.reason, "rank": trade.rank,
            "trader": trade.trader, "win_rate": trade.win_rate,
            "imbalance": trade.imbalance, "status": trade.status,
        }
        with open(COPY_LOG_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        logger.error(f"Failed to log copy trade: {e}")

# =============================================================================
# OPTION A: LIVE LEADERBOARD RANKER
# =============================================================================

class LiveLeaderboardCopier:
    """
    Fetches ALL available traders from predicting.top.
    Ranks them live by a consistency score = win_rate × log(total_trades).
    Follows top N dynamically — rankings update every 5 minutes.
    Copies any new positions from top traders at $12.50 flat.
    """

    def __init__(self):
        self.all_traders:    List[RankedTrader] = []
        self.top_traders:    List[RankedTrader] = []
        self.known_positions: Dict[str, set]    = {}
        self.copy_count      = 0
        self.running         = False
        self.last_scan       = None
        self.status          = "IDLE"
        self.scan_count      = 0
        self._lock           = threading.Lock()

    def _consistency_score(self, win_rate: float, total_trades: int) -> float:
        """
        Score = win_rate × log(trades)
        Rewards both high win rate AND volume of evidence.
        A trader with 90% WR on 10 trades scores lower than
        one with 80% WR on 200 trades.
        """
        import math
        if total_trades < COPY_MIN_TRADES:
            return 0.0
        return win_rate * math.log(max(total_trades, 1))

    def fetch_and_rank(self) -> List[RankedTrader]:
        """Fetch all traders and rank by consistency score."""
        traders = []
        try:
            req = urllib.request.Request(
                LEADERBOARD_URL,
                headers={"User-Agent": "BBVBot/2.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            raw = data.get("traders", [])
            for t in raw:
                wr     = float(t.get("win_rate", 0))
                total  = int(t.get("total_trades", 0))
                wins   = int(t.get("wins", int(wr * total)))
                losses = total - wins
                score  = self._consistency_score(wr, total)

                if total >= COPY_MIN_TRADES and score > 0:
                    traders.append(RankedTrader(
                        rank         = 0,
                        username     = t.get("username", "unknown"),
                        win_rate     = wr,
                        total_trades = total,
                        total_pnl    = float(t.get("total_pnl", 0)),
                        win_count    = wins,
                        loss_count   = losses,
                        consistency  = round(score, 4),
                        last_updated = datetime.now(timezone.utc).isoformat(),
                    ))

            # Sort by consistency score descending
            traders.sort(key=lambda x: x.consistency, reverse=True)
            for i, t in enumerate(traders):
                t.rank = i + 1

            logger.info(f"[SOCIAL] Ranked {len(traders)} traders | Top: {traders[0].username if traders else 'none'} ({traders[0].win_rate*100:.1f}% WR)" if traders else "[SOCIAL] No traders found")

        except Exception as e:
            logger.warning(f"[SOCIAL] Leaderboard fetch failed: {e}")

        return traders

    def fetch_positions(self, username: str) -> List[dict]:
        """Fetch open positions for a trader."""
        try:
            url = f"https://predicting.top/api/traders/{username}/positions?platform=kalshi&status=open"
            req = urllib.request.Request(url, headers={"User-Agent": "BBVBot/2.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return data.get("positions", [])
        except Exception:
            return []

    def check_and_copy(self, trades_out: list):
        """Check top N traders for new positions."""
        for trader in self.top_traders:
            try:
                positions = self.fetch_positions(trader.username)
                known     = self.known_positions.get(trader.username, set())
                new_pos   = [p for p in positions if p.get("ticker") not in known]

                for pos in new_pos:
                    ticker    = pos.get("ticker", "")
                    direction = pos.get("side", "YES").upper()
                    price     = float(pos.get("price", 0))

                    if not ticker:
                        continue
                    if not (COPY_PRICE_MIN <= price <= COPY_PRICE_MAX):
                        logger.info(f"[SOCIAL] Skip {ticker} price={price:.2f} out of range")
                        continue

                    contracts = max(1, int(COPY_FLAT_USD / max(price, COPY_PRICE_MIN)))
                    cost      = round(contracts * price, 2)

                    trade = CopyTrade(
                        source    = "SOCIAL",
                        ticker    = ticker,
                        direction = direction,
                        contracts = contracts,
                        cost      = cost,
                        reason    = f"#{trader.rank} {trader.username} | {trader.win_rate*100:.1f}% WR | {trader.total_trades} trades | score={trader.consistency:.2f}",
                        rank      = trader.rank,
                        trader    = trader.username,
                        win_rate  = trader.win_rate,
                        imbalance = None,
                        timestamp = datetime.now(timezone.utc).isoformat(),
                    )

                    logger.info(f"[SOCIAL] COPY #{trader.rank} {trader.username}: {ticker} {direction} {contracts}x ${cost}")
                    log_copy_trade(trade)
                    trades_out.append(trade)
                    self.copy_count += 1
                    known.add(ticker)

                self.known_positions[trader.username] = known

            except Exception as e:
                logger.error(f"[SOCIAL] Error checking {trader.username}: {e}")

    def run(self):
        self.running = True
        self.status  = "RUNNING"
        logger.info("[SOCIAL] Live leaderboard ranker started")

        while self.running:
            try:
                ranked = self.fetch_and_rank()
                with self._lock:
                    self.all_traders = ranked
                    self.top_traders = ranked[:COPY_TOP_N]
                self.last_scan  = datetime.now(timezone.utc).isoformat()
                self.scan_count += 1
                trades_out = []
                if self.top_traders:
                    self.check_and_copy(trades_out)
            except Exception as e:
                logger.error(f"[SOCIAL] Run error: {e}")

            time.sleep(COPY_SCAN_INTERVAL_S)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def get_status(self) -> dict:
        with self._lock:
            traders = self.all_traders[:20]
        return {
            "status":      self.status,
            "copy_count":  self.copy_count,
            "last_scan":   self.last_scan,
            "scan_count":  self.scan_count,
            "top_n":       COPY_TOP_N,
            "total_ranked": len(self.all_traders),
            "leaderboard": [
                {
                    "rank":        t.rank,
                    "username":    t.username,
                    "win_rate":    round(t.win_rate, 4),
                    "win_rate_pct": f"{t.win_rate*100:.1f}%",
                    "total_trades": t.total_trades,
                    "wins":        t.win_count,
                    "losses":      t.loss_count,
                    "total_pnl":   round(t.total_pnl, 2),
                    "consistency": t.consistency,
                    "following":   t.rank <= COPY_TOP_N,
                }
                for t in traders
            ],
        }

# =============================================================================
# OPTION B: INFORMED FLOW DETECTOR
# =============================================================================

class InformedFlowDetector:
    """
    Scans Kalshi order books every 60s for imbalance signals.
    Fires when one side has 3x+ the volume of the other.
    """

    def __init__(self):
        self.signals:   List[FlowSignal] = []
        self.flow_count = 0
        self.running    = False
        self.last_scan  = None
        self.status     = "IDLE"
        self._lock      = threading.Lock()

    def fetch_orderbook(self, ticker: str) -> Optional[dict]:
        try:
            url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    def detect(self, ticker: str, ob: dict) -> Optional[FlowSignal]:
        try:
            yes_bids = ob.get("orderbook", {}).get("yes", [])
            no_bids  = ob.get("orderbook", {}).get("no",  [])
            yes_vol  = sum(l[1] for l in yes_bids if len(l) >= 2)
            no_vol   = sum(l[1] for l in no_bids  if len(l) >= 2)

            if yes_vol + no_vol < FLOW_MIN_VOLUME:
                return None

            if no_vol > 0 and yes_vol / no_vol >= FLOW_IMBALANCE_THRESH:
                direction = "YES"
                imbalance = round(yes_vol / no_vol, 2)
            elif yes_vol > 0 and no_vol / yes_vol >= FLOW_IMBALANCE_THRESH:
                direction = "NO"
                imbalance = round(no_vol / yes_vol, 2)
            else:
                return None

            yes_bid = yes_bids[0][0] / 100 if yes_bids else 0
            if not (COPY_PRICE_MIN <= yes_bid <= COPY_PRICE_MAX):
                return None

            return FlowSignal(
                ticker    = ticker,
                direction = direction,
                imbalance = imbalance,
                yes_vol   = yes_vol,
                no_vol    = no_vol,
                yes_bid   = round(yes_bid, 3),
                timestamp = datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            return None

    def scan(self, tickers: List[str]) -> List[CopyTrade]:
        trades_out = []
        new_signals = []

        for ticker in tickers[:50]:
            try:
                ob = self.fetch_orderbook(ticker)
                if not ob:
                    continue
                sig = self.detect(ticker, ob)
                if sig:
                    new_signals.append(sig)
                    self.flow_count += 1
                    price     = sig.yes_bid if sig.direction == "YES" else round(1 - sig.yes_bid, 3)
                    contracts = max(1, int(COPY_FLAT_USD / max(price, COPY_PRICE_MIN)))
                    cost      = round(contracts * price, 2)

                    trade = CopyTrade(
                        source    = "FLOW",
                        ticker    = ticker,
                        direction = sig.direction,
                        contracts = contracts,
                        cost      = cost,
                        reason    = f"Order book {sig.imbalance}x imbalance | YES:{sig.yes_vol} NO:{sig.no_vol}",
                        rank      = None,
                        trader    = None,
                        win_rate  = None,
                        imbalance = sig.imbalance,
                        timestamp = sig.timestamp,
                    )
                    logger.info(f"[FLOW] {ticker} {sig.direction} imbalance={sig.imbalance}x ${cost}")
                    log_copy_trade(trade)
                    trades_out.append(trade)
                time.sleep(0.1)
            except Exception:
                pass

        with self._lock:
            self.signals = new_signals[-20:]
        self.last_scan = datetime.now(timezone.utc).isoformat()
        return trades_out

    def run(self, get_tickers_fn):
        self.running = True
        self.status  = "RUNNING"
        logger.info("[FLOW] Informed flow detector started")
        while self.running:
            try:
                tickers = get_tickers_fn()
                if tickers:
                    self.scan(tickers)
            except Exception as e:
                logger.error(f"[FLOW] Error: {e}")
            time.sleep(FLOW_SCAN_INTERVAL_S)

    def start(self, get_tickers_fn):
        threading.Thread(target=self.run, args=(get_tickers_fn,), daemon=True).start()

    def get_status(self) -> dict:
        with self._lock:
            signals = list(self.signals[-10:])
        return {
            "status":     self.status,
            "flow_count": self.flow_count,
            "last_scan":  self.last_scan,
            "recent_signals": [
                {"ticker": s.ticker, "direction": s.direction,
                 "imbalance": s.imbalance, "yes_bid": s.yes_bid,
                 "timestamp": s.timestamp}
                for s in signals
            ],
        }

# =============================================================================
# COPY TRADE LOG HELPERS
# =============================================================================

def load_copy_trades(limit: int = 100) -> List[dict]:
    trades = []
    try:
        with open(COPY_LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
        trades.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return trades[:limit]
    except FileNotFoundError:
        return []

def copy_trade_stats(trades: List[dict]) -> dict:
    if not trades:
        return {"total": 0, "social": 0, "flow": 0, "total_cost": 0.0, "avg_cost": 0.0}
    social = [t for t in trades if t.get("source") == "SOCIAL"]
    flow   = [t for t in trades if t.get("source") == "FLOW"]
    total_cost = sum(t.get("cost", 0) for t in trades)
    return {
        "total":      len(trades),
        "social":     len(social),
        "flow":       len(flow),
        "total_cost": round(total_cost, 2),
        "avg_cost":   round(total_cost / len(trades), 2),
    }
