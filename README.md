# crypto-copy-trading-ai

A KuCoin copy-trading framework that discovers and mirrors top smart-money traders using on-chain/exchange data plus an ML filter and regime classifier. Built for small accounts with conservative risk sizing.

## Architecture

```
smart_copy_ai/
├── coinglass.py        # CoinGlass derivatives data client
├── config.py           # Typed config (risk, regime, ML, Telegram)
├── executor.py         # Order execution layer (KuCoin spot + futures)
├── features.py         # Feature engineering for ML filter
├── ml_filter.py        # ML-based signal filtering
├── pipeline.py         # Discovery → filter → execute pipeline
├── regime.py           # Bull/Bear/Chop regime classifier
├── risk_manager.py     # Position sizing matrix (regime × ML confidence)
├── signals.py          # Signal generation from top-trader activity
├── telegram_bot.py     # Alerts + control interface
└── wallet_monitor.py   # Hyperliquid + Coinglass wallet tracking
```

Top-level scripts:

- `find_top_traders.py` — scrape Hyperliquid + Coinglass leaderboards
- `top_traders.py` — rank and curate wallet list
- `main.py` — run the full copy-trading loop
- `test_kucoin_copytrading.py` — sanity-check KuCoin integration

## Risk design

The bot is tuned for a **<€500 starting capital** with a position sizing matrix keyed by (ML action × market regime). Full-risk positions sit at 1–1.5% of capital; reduced positions scale down from there.

## Quick start

```bash
pip install -r requirements.txt   # if requirements.txt exists; otherwise review imports in smart_copy_ai/

# Configure credentials via environment
export KUCOIN_MASTER_KEY=...
export KUCOIN_MASTER_SECRET=...
export KUCOIN_MASTER_PASSPHRASE=...
export COINGLASS_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...

# Paper trade first
export KUCOIN_SANDBOX=true
python main.py
```

## ⚠️ Disclaimer

This software is provided **for educational and research purposes only** and is **not** financial advice. Copy trading carries high risk — the traders being copied may be making poor decisions or their past performance may not continue. KuCoin futures trading involves leverage and can result in rapid total loss of capital. The author assumes no liability for losses arising from use of this software. Use sandbox mode for all exploration.
