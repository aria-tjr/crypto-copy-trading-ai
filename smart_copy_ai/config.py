"""
Configuration — Central config for the entire pipeline.
=========================================================
All API keys, risk params, thresholds, and wallet whitelist in one place.
Environment variables override defaults for security.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum

# ─── Enums ───────────────────────────────────────────────────────────────────

class MarketRegime(Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOL = "HIGH_VOL"

class MLAction(Enum):
    FULL = "FULL"
    REDUCE_75 = "REDUCE_75"
    REDUCE_50 = "REDUCE_50"
    BLOCK = "BLOCK"

class SignalSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"

# ─── API Keys ────────────────────────────────────────────────────────────────

@dataclass
class KuCoinConfig:
    # Sub-account key with LeadtradeFutures permission
    api_key: str = os.getenv("KUCOIN_API_KEY", "")
    api_secret: str = os.getenv("KUCOIN_API_SECRET", "")
    api_passphrase: str = os.getenv("KUCOIN_API_PASSPHRASE", "")
    # Master account key with Futures permission
    master_api_key: str = os.getenv("KUCOIN_MASTER_KEY", "")
    master_api_secret: str = os.getenv("KUCOIN_MASTER_SECRET", "")
    master_api_passphrase: str = os.getenv("KUCOIN_MASTER_PASSPHRASE", "")
    # Endpoints
    spot_base_url: str = "https://api.kucoin.com"
    futures_base_url: str = "https://api-futures.kucoin.com"
    # Use sandbox for paper trading
    sandbox: bool = os.getenv("KUCOIN_SANDBOX", "false").lower() == "true"

@dataclass
class CoinGlassConfig:
    api_key: str = os.getenv("COINGLASS_API_KEY", "")
    base_url: str = "https://open-api-v4.coinglass.com"

@dataclass
class TelegramConfig:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled: bool = bool(os.getenv("TELEGRAM_BOT_TOKEN", ""))

@dataclass
class HyperliquidConfig:
    info_url: str = "https://api.hyperliquid.xyz/info"
    leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# ─── Risk Parameters ────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    """All risk management parameters — matched to <€500 capital."""
    
    # Starting capital in USD
    initial_capital: float = float(os.getenv("INITIAL_CAPITAL", "500"))
    
    # Position sizing matrix: ML_action → regime → risk_pct of capital
    # Defines what percentage of total capital is risked per trade
    position_size_matrix: Dict = field(default_factory=lambda: {
        MLAction.FULL: {
            MarketRegime.BULL: 0.015,       # 1.5%
            MarketRegime.BEAR: 0.010,       # 1.0%
            MarketRegime.SIDEWAYS: 0.015,   # 1.5%
            MarketRegime.HIGH_VOL: 0.010,   # 1.0%
        },
        MLAction.REDUCE_75: {
            MarketRegime.BULL: 0.011,
            MarketRegime.BEAR: 0.0075,
            MarketRegime.SIDEWAYS: 0.011,
            MarketRegime.HIGH_VOL: 0.0075,
        },
        MLAction.REDUCE_50: {
            MarketRegime.BULL: 0.0075,
            MarketRegime.BEAR: 0.005,
            MarketRegime.SIDEWAYS: 0.0075,
            MarketRegime.HIGH_VOL: 0.005,
        },
        MLAction.BLOCK: {
            MarketRegime.BULL: 0.0,
            MarketRegime.BEAR: 0.0,
            MarketRegime.SIDEWAYS: 0.0,
            MarketRegime.HIGH_VOL: 0.0,
        },
    })
    
    # Global limits (non-negotiable)
    max_daily_risk_pct: float = 0.04         # 4% max daily risk
    max_open_positions_normal: int = 3        # Normal market
    max_open_positions_high_vol: int = 2      # High volatility
    max_leverage: int = 20                    # Hard cap
    
    # Stop loss / Take profit defaults
    default_sl_pct: float = 0.02             # 2% stop loss
    default_tp_pcts: List[float] = field(default_factory=lambda: [0.03, 0.06])  # 3% and 6%
    tp_partial_pcts: List[float] = field(default_factory=lambda: [0.5, 0.5])    # 50% at each TP
    
    # Trailing stop
    trail_sl_activation_pct: float = 0.02    # Move SL to breakeven at +2%
    trail_sl_offset_pct: float = 0.005       # 0.5% trailing offset
    
    # Circuit breaker
    circuit_breaker_daily_loss_pct: float = 0.03   # -3% daily → 24h full stop
    circuit_breaker_cooldown_hours: int = 24
    
    # Consecutive loss protection
    max_consecutive_losses: int = 5           # Reduce size after 5 losses
    loss_streak_size_reduction: float = 0.5   # Cut to 50% size

# ─── Wallet Whitelist ────────────────────────────────────────────────────────

@dataclass
class WalletConfig:
    """Top 5 wallets to copy — re-ranked every Friday."""
    
    # Wallet whitelist: address → metadata
    # Updated from Hyperliquid leaderboard + Maestro tracking
    whitelist: Dict[str, Dict] = field(default_factory=lambda: {
        # Top 5 from Hyperliquid leaderboard (auto-discovered)
        "0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00": {"name": "Whale#1", "alltime_pnl": 195_600_000},
        "0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36": {"name": "Whale#2", "alltime_pnl": 122_200_000},
        "0x880ac484a1743862989a441d6d867238c7aa311c": {"name": "SilkBtc", "alltime_pnl": 114_200_000},
        "0x20c2d95a3dfdca9e9ad12794d5fa6fad99da44f5": {"name": "Whale#3", "alltime_pnl": 122_200_000},
        "0x51156f7002c4f74f4956c9e0f2b7bfb6e9dbfac2": {"name": "jefe", "alltime_pnl": 71_700_000},
    })
    
    # Ranking criteria weights (for weekly re-rank)
    ranking_weights: Dict[str, float] = field(default_factory=lambda: {
        "sharpe_ratio": 0.40,
        "win_rate": 0.30,
        "max_drawdown": 0.20,  # Uses 1/max_dd
        "trade_frequency": 0.10,
    })
    
    # Address → display name alias
    aliases: Dict[str, str] = field(default_factory=lambda: {
        "0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00": "Whale#1",
        "0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36": "Whale#2",
        "0x880ac484a1743862989a441d6d867238c7aa311c": "SilkBtc",
        "0x20c2d95a3dfdca9e9ad12794d5fa6fad99da44f5": "Whale#3",
        "0x51156f7002c4f74f4956c9e0f2b7bfb6e9dbfac2": "jefe",
    })
    
    # Minimum thresholds to stay in whitelist
    min_sharpe: float = 1.5
    min_win_rate: float = 0.60
    max_drawdown_pct: float = 0.15   # Drop if DD > 15%
    min_trades_30d: int = 10

# ─── CoinGlass Veto Thresholds ──────────────────────────────────────────────

@dataclass
class CoinGlassVetoConfig:
    """Layer 3 — Hard veto conditions."""
    
    # LONG signal BLOCK conditions
    long_block_ratio_threshold: float = 0.85    # Block if LS ratio < 0.85
    long_block_funding_threshold: float = 0.0005  # AND funding > +0.05%
    
    # SHORT signal BLOCK conditions
    short_block_ratio_threshold: float = 1.15   # Block if LS ratio > 1.15
    short_block_funding_threshold: float = -0.0005  # AND funding < -0.05%
    
    # Confidence boost
    strong_long_ratio: float = 1.3    # Boost confidence × 1.2
    strong_short_ratio: float = 0.7   # Boost confidence × 1.2
    confidence_boost: float = 1.2

# ─── Regime Detection ────────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    """Market regime detection parameters."""
    
    # BTC price change thresholds (24h)
    bull_threshold_pct: float = 0.05     # +5%
    bear_threshold_pct: float = -0.05    # -5%
    sideways_band_pct: float = 0.02      # ±2%
    
    # Volume thresholds
    volume_up_multiplier: float = 1.3    # 30% above average
    volume_down_multiplier: float = 0.7  # 30% below average
    
    # Volatility (ATR-based)
    high_vol_atr_multiplier: float = 2.0  # ATR > 2× 30d average
    
    # Detection interval
    check_interval_hours: int = 24

# ─── ML Model Config ────────────────────────────────────────────────────────

@dataclass
class MLConfig:
    """XGBoost classifier configuration."""
    
    model_dir: str = os.path.join(os.path.dirname(__file__), "..", "models")
    
    # Feature count
    n_features: int = 35
    
    # Output classes
    classes: List[str] = field(default_factory=lambda: ["BLOCK", "REDUCE_50", "REDUCE_75", "FULL"])
    
    # Training
    retrain_day: str = "friday"          # Weekly retrain
    retrain_hour: int = 0                # At midnight
    lookback_days: int = 180             # 6 months of training data
    test_size: float = 0.2
    
    # XGBoost hyperparams
    xgb_params: Dict = field(default_factory=lambda: {
        "n_estimators": 200,
        "max_depth": 6,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 3,
        "objective": "multi:softprob",
        "num_class": 4,
        "eval_metric": "mlogloss",
        "use_label_encoder": False,
    })
    
    # Minimum confidence to act (below this → BLOCK)
    min_confidence: float = 0.55

# ─── Pipeline Config ─────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    """Main pipeline loop configuration."""
    
    # Loop interval
    signal_loop_interval_sec: int = 30
    position_check_interval_sec: int = 60
    
    # Paper trading mode
    paper_trading: bool = True  # START IN PAPER MODE
    
    # Database
    db_path: str = os.path.join(os.path.dirname(__file__), "..", "data", "trades.db")
    
    # Logging
    log_dir: str = os.path.join(os.path.dirname(__file__), "..", "logs")
    log_level: str = "INFO"

# ─── Master Config ───────────────────────────────────────────────────────────

@dataclass
class Config:
    """Master configuration — single access point for everything."""
    
    kucoin: KuCoinConfig = field(default_factory=KuCoinConfig)
    coinglass: CoinGlassConfig = field(default_factory=CoinGlassConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    hyperliquid: HyperliquidConfig = field(default_factory=HyperliquidConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    wallets: WalletConfig = field(default_factory=WalletConfig)
    coinglass_veto: CoinGlassVetoConfig = field(default_factory=CoinGlassVetoConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)


# Singleton instance
config = Config()
