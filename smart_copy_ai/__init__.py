"""
Smart Copy AI — Hybrid Copy Trading System
===========================================
Architecture: Raw Copy Trading (80%) + ML Filter (20%) + CoinGlass Validation
Target: Sharpe >2.0 · Winrate >72% · Max Drawdown <12%

Modules:
  config.py         — Central configuration
  signals.py        — Signal format, whitelist, SQLite storage
  wallet_monitor.py — Hyperliquid wallet position detection
  features.py       — 35-feature engineering for ML
  ml_filter.py      — XGBoost classifier (FULL/REDUCE/BLOCK)
  coinglass.py      — Top trader ratio + funding rate veto
  risk_manager.py   — Position sizing, limits, circuit breaker
  executor.py       — KuCoin Futures order execution
  regime.py         — Market regime detection (BULL/BEAR/SIDEWAYS/HIGH_VOL)
  telegram_bot.py   — Alerts and daily reports
  pipeline.py       — Main 30-second loop orchestrator
"""

__version__ = "0.1.0"
