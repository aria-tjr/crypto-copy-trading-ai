"""
Signal Intake — Standardized signal format, whitelist validation, SQLite storage.
=================================================================================
Handles incoming signals from all sources (Maestro, Hyperliquid wallets, manual)
and normalizes them into a unified format for the pipeline.
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional

from .config import config, SignalSide, MLAction

logger = logging.getLogger(__name__)


# ─── Signal Data Model ──────────────────────────────────────────────────────

@dataclass
class Signal:
    """Standardized signal format across all sources."""
    
    id: str = ""
    symbol: str = ""               # e.g. "BTCUSDTM"
    side: str = "LONG"             # "LONG" or "SHORT"
    entry_price: float = 0.0       # Suggested entry
    sl_pct: float = -0.02          # Stop loss % (negative)
    tp_pcts: List[float] = field(default_factory=lambda: [0.03, 0.06])
    leverage: int = 5
    source: str = ""               # "maestro", "hyperliquid", "manual"
    wallet_id: str = ""            # Source wallet address
    timestamp: float = 0.0
    raw_data: Dict = field(default_factory=dict)   # Original data from source
    
    # Pipeline fills these
    ml_action: str = ""            # FULL, REDUCE_75, REDUCE_50, BLOCK
    ml_confidence: float = 0.0
    coinglass_vetoed: bool = False
    final_risk_pct: float = 0.0    # Actual risk % after all filters
    final_size: float = 0.0        # Position size in contracts/coins
    
    # Execution tracking
    status: str = "PENDING"        # PENDING → APPROVED → EXECUTED → CLOSED → BLOCKED
    order_id: str = ""
    fill_price: float = 0.0
    realized_pnl: float = 0.0
    closed_at: float = 0.0
    close_reason: str = ""         # "TP1", "TP2", "SL", "TRAIL", "MANUAL"
    
    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = time.time()
    
    def to_dict(self) -> Dict:
        d = asdict(self)
        d["tp_pcts"] = json.dumps(d["tp_pcts"])
        d["raw_data"] = json.dumps(d["raw_data"]) if isinstance(d["raw_data"], dict) else str(d["raw_data"])
        return d
    
    @classmethod
    def from_dict(cls, d: Dict) -> "Signal":
        if isinstance(d.get("tp_pcts"), str):
            d["tp_pcts"] = json.loads(d["tp_pcts"])
        if isinstance(d.get("raw_data"), str):
            try:
                d["raw_data"] = json.loads(d["raw_data"])
            except (json.JSONDecodeError, TypeError):
                d["raw_data"] = {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
    
    @property
    def sl_price(self) -> float:
        if self.side == "LONG":
            return self.entry_price * (1 + self.sl_pct)
        return self.entry_price * (1 - self.sl_pct)
    
    @property
    def tp_prices(self) -> List[float]:
        if self.side == "LONG":
            return [self.entry_price * (1 + tp) for tp in self.tp_pcts]
        return [self.entry_price * (1 - tp) for tp in self.tp_pcts]
    
    def summary(self) -> str:
        return (
            f"[{self.id}] {self.side} {self.symbol} @ {self.entry_price:.2f} "
            f"| SL: {self.sl_pct*100:+.1f}% | TP: {[f'{t*100:+.1f}%' for t in self.tp_pcts]} "
            f"| {self.leverage}x | src: {self.source} | status: {self.status}"
        )


# ─── Whitelist Validator ────────────────────────────────────────────────────

class WhitelistValidator:
    """Validates signals come from approved wallets only."""
    
    def __init__(self):
        self.whitelist = config.wallets.whitelist
    
    def is_whitelisted(self, wallet_id: str) -> bool:
        """Check if wallet is in the approved list."""
        if not wallet_id:
            return False
        return wallet_id.lower() in {k.lower() for k in self.whitelist.keys()}
    
    def validate_signal(self, signal: Signal) -> bool:
        """Validate a signal passes whitelist check."""
        # Manual signals bypass whitelist
        if signal.source == "manual":
            return True
        
        if not self.is_whitelisted(signal.wallet_id):
            signal.status = "BLOCKED"
            signal.close_reason = "wallet_not_whitelisted"
            return False
        
        return True
    
    def update_whitelist(self, new_whitelist: Dict[str, Dict]):
        """Update the wallet whitelist (e.g., after weekly re-rank)."""
        self.whitelist = new_whitelist
        config.wallets.whitelist = new_whitelist


# ─── Signal Database (SQLite) ───────────────────────────────────────────────

class SignalDB:
    """SQLite storage for all signals and trade history."""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.pipeline.db_path
        self._ensure_dir()
        self._init_db()
    
    def _ensure_dir(self):
        import os
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
    
    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL,
                    sl_pct REAL,
                    tp_pcts TEXT,
                    leverage INTEGER,
                    source TEXT,
                    wallet_id TEXT,
                    timestamp REAL,
                    ml_action TEXT,
                    ml_confidence REAL,
                    coinglass_vetoed INTEGER DEFAULT 0,
                    final_risk_pct REAL,
                    final_size REAL,
                    status TEXT DEFAULT 'PENDING',
                    order_id TEXT,
                    fill_price REAL,
                    realized_pnl REAL,
                    closed_at REAL,
                    close_reason TEXT,
                    raw_data TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # ── Migrate existing DB: add raw_data if missing ─────────────
            try:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()]
                if "raw_data" not in cols:
                    conn.execute("ALTER TABLE signals ADD COLUMN raw_data TEXT DEFAULT '{}'")
                    logger.info("✅ Migrated signals table: added raw_data column")
            except Exception as e:
                logger.warning(f"Migration check skipped: {e}")
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    total_risk_used REAL DEFAULT 0,
                    blocked_by_ml INTEGER DEFAULT 0,
                    blocked_by_coinglass INTEGER DEFAULT 0,
                    circuit_breaker_triggered INTEGER DEFAULT 0,
                    regime TEXT,
                    capital_start REAL,
                    capital_end REAL
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wallet_performance (
                    wallet_id TEXT,
                    date TEXT,
                    sharpe_ratio REAL,
                    win_rate REAL,
                    max_drawdown REAL,
                    trade_count INTEGER,
                    total_pnl REAL,
                    rank_score REAL,
                    PRIMARY KEY (wallet_id, date)
                )
            """)
            
            conn.commit()
    
    def save_signal(self, signal: Signal):
        """Insert or update a signal."""
        d = signal.to_dict()
        d["coinglass_vetoed"] = int(d["coinglass_vetoed"])
        
        cols = ", ".join(d.keys())
        placeholders = ", ".join(["?"] * len(d))
        updates = ", ".join([f"{k}=excluded.{k}" for k in d.keys() if k != "id"])
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"INSERT INTO signals ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                list(d.values())
            )
            conn.commit()
    
    def get_signal(self, signal_id: str) -> Optional[Signal]:
        """Get a signal by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
            if row:
                d = dict(row)
                d["coinglass_vetoed"] = bool(d["coinglass_vetoed"])
                return Signal.from_dict(d)
        return None
    
    def get_pending_signals(self) -> List[Signal]:
        """Get all signals waiting to be processed."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM signals WHERE status = 'PENDING' ORDER BY timestamp ASC"
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["coinglass_vetoed"] = bool(d["coinglass_vetoed"])
                results.append(Signal.from_dict(d))
            return results
    
    def get_open_positions(self) -> List[Signal]:
        """Get all currently open (executed) positions."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM signals WHERE status = 'EXECUTED' ORDER BY timestamp ASC"
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["coinglass_vetoed"] = bool(d["coinglass_vetoed"])
                results.append(Signal.from_dict(d))
            return results
    
    def get_daily_pnl(self, date: Optional[str] = None) -> float:
        """Get total PnL for a given date (default: today)."""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM signals "
                "WHERE date(datetime(closed_at, 'unixepoch')) = ? AND status = 'CLOSED'",
                (date,)
            ).fetchone()
            return row[0] if row else 0.0
    
    def get_daily_risk_used(self, date: Optional[str] = None) -> float:
        """Get total risk used today."""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(final_risk_pct), 0) FROM signals "
                "WHERE date(datetime(timestamp, 'unixepoch')) = ? "
                "AND status IN ('EXECUTED', 'CLOSED', 'APPROVED')",
                (date,)
            ).fetchone()
            return row[0] if row else 0.0
    
    def count_open_positions(self) -> int:
        """Count currently open positions."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status = 'EXECUTED'"
            ).fetchone()
            return row[0] if row else 0
    
    def get_recent_trades(self, n: int = 20) -> List[Signal]:
        """Get the N most recent closed trades."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM signals WHERE status = 'CLOSED' "
                "ORDER BY closed_at DESC LIMIT ?",
                (n,)
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["coinglass_vetoed"] = bool(d["coinglass_vetoed"])
                results.append(Signal.from_dict(d))
            return results
    
    def get_consecutive_losses(self) -> int:
        """Count current consecutive losing trades."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT realized_pnl FROM signals WHERE status = 'CLOSED' "
                "ORDER BY closed_at DESC LIMIT 20"
            ).fetchall()
            
            streak = 0
            for row in rows:
                if row["realized_pnl"] < 0:
                    streak += 1
                else:
                    break
            return streak
    
    def save_daily_stats(self, date: str, stats: Dict):
        """Save daily performance stats."""
        cols = ", ".join(stats.keys())
        placeholders = ", ".join(["?"] * len(stats))
        updates = ", ".join([f"{k}=excluded.{k}" for k in stats.keys() if k != "date"])
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"INSERT INTO daily_stats (date, {cols}) VALUES (?, {placeholders}) "
                f"ON CONFLICT(date) DO UPDATE SET {updates}",
                [date] + list(stats.values())
            )
            conn.commit()
