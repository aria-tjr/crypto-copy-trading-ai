#!/usr/bin/env python3
"""
Smart Copy AI — Main Entry Point
==================================
Hybrid copy trading system with ML filtering and CoinGlass veto.

Usage:
  python main.py                    # Start pipeline (paper mode)
  python main.py --live             # Start pipeline (live mode)  
  python main.py --status           # Show system status
  python main.py --discover         # Discover top wallets from leaderboard
  python main.py --once             # Run single iteration (for testing)
"""

import argparse
import json
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def setup_logging(log_dir: str = "logs", level: str = "INFO"):
    """Configure logging to both console and file."""
    os.makedirs(log_dir, exist_ok=True)
    
    from datetime import datetime
    log_file = os.path.join(log_dir, f"bot_{datetime.now().strftime('%Y%m%d')}.log")
    
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    
    # Suppress noisy urllib logs
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_start(args):
    """Start the main pipeline."""
    from smart_copy_ai.config import config
    
    if args.live:
        config.pipeline.paper_trading = False
        print("⚠️  LIVE TRADING MODE — real orders will be placed!")
        confirm = input("Type 'YES' to confirm: ")
        if confirm != "YES":
            print("Aborted.")
            return
    
    setup_logging(config.pipeline.log_dir, config.pipeline.log_level)
    
    from smart_copy_ai.pipeline import Pipeline
    
    pipeline = Pipeline()
    pipeline.start()


def cmd_once(args):
    """Run a single pipeline iteration."""
    from smart_copy_ai.config import config
    setup_logging(config.pipeline.log_dir, "DEBUG")
    
    from smart_copy_ai.pipeline import Pipeline
    
    pipeline = Pipeline()
    status = pipeline.run_once()
    
    print("\n📊 Status:")
    print(json.dumps(status, indent=2, default=str))


def cmd_status(args):
    """Show current system status."""
    from smart_copy_ai.config import config
    from smart_copy_ai.signals import SignalDB
    from smart_copy_ai.risk_manager import RiskManager
    
    db = SignalDB()
    risk = RiskManager(db)
    
    print("\n" + "=" * 50)
    print("📊 SMART COPY AI — System Status")
    print("=" * 50)
    
    # Risk status
    rs = risk.get_risk_status()
    print(f"\n💰 Capital: ${rs['capital']:,.2f}")
    print(f"📊 Regime: {rs['regime']}")
    print(f"📈 Daily PnL: ${rs['daily_pnl']:+,.2f} ({rs['daily_pnl_pct']:+.2f}%)")
    print(f"⚖️  Risk used: {rs['daily_risk_used']*100:.2f}% / {config.risk.max_daily_risk_pct*100:.0f}%")
    print(f"📦 Open positions: {rs['open_positions']} / {rs['max_positions']}")
    print(f"📉 Consecutive losses: {rs['consecutive_losses']}")
    print(f"🛑 Circuit breaker: {'ACTIVE' if rs['circuit_breaker_active'] else 'off'}")
    
    # Recent trades
    trades = db.get_recent_trades(5)
    if trades:
        print(f"\n📋 Recent trades:")
        for t in trades:
            emoji = "✅" if t.realized_pnl > 0 else "❌"
            print(f"  {emoji} {t.side} {t.symbol} ${t.realized_pnl:+,.2f} ({t.close_reason})")
    
    # Mode
    mode = "📝 PAPER" if config.pipeline.paper_trading else "🔴 LIVE"
    print(f"\n🤖 Mode: {mode}")
    print(f"👛 Wallets tracked: {len(config.wallets.whitelist)}")


def cmd_discover(args):
    """Discover top wallets from Hyperliquid leaderboard."""
    from smart_copy_ai.wallet_monitor import WalletMonitor
    
    print("🔍 Fetching Hyperliquid leaderboard...")
    
    top = WalletMonitor.discover_top_wallets(
        min_all_time_pnl=args.min_pnl,
        min_month_roi=args.min_roi / 100,
        top_n=args.top_n,
    )
    
    if not top:
        print("❌ No qualifying traders found.")
        return
    
    print(f"\n🏆 TOP {len(top)} TRADERS")
    print("=" * 80)
    
    for i, t in enumerate(top, 1):
        print(
            f"\n{i}. {t['name']}\n"
            f"   Address: {t['address']}\n"
            f"   Account: ${t['account_value']:,.0f}\n"
            f"   All-time PnL: ${t['alltime_pnl']:,.0f}\n"
            f"   Month ROI: {t['month_roi']*100:+.1f}%\n"
            f"   Week PnL: ${t['week_pnl']:+,.0f}"
        )
    
    print(f"\n{'─' * 80}")
    print("💡 Add these to config.py → WalletConfig.whitelist to start monitoring.")
    
    # Optionally save to file
    if args.save:
        out = os.path.join(os.path.dirname(__file__), "discovered_wallets.json")
        with open(out, "w") as f:
            json.dump(top, f, indent=2)
        print(f"💾 Saved to {out}")


def cmd_test_kucoin(args):
    """Test KuCoin API connectivity."""
    from smart_copy_ai.executor import KuCoinExecutor
    from smart_copy_ai.signals import SignalDB
    
    print("🔑 Testing KuCoin API...")
    
    db = SignalDB()
    executor = KuCoinExecutor(db, paper=True)
    
    # Test account overview
    overview = executor.get_account_overview()
    if overview:
        print(f"✅ Account connected: ${float(overview.get('accountEquity', 0)):,.2f}")
    else:
        print("❌ Failed to connect to KuCoin")
    
    # Test ticker
    ticker = executor.get_ticker("XBTUSDTM")
    if ticker:
        print(f"✅ BTC price: ${float(ticker.get('price', 0)):,.2f}")
    else:
        print("❌ Failed to get ticker")


def main():
    parser = argparse.ArgumentParser(
        description="Smart Copy AI — Hybrid Copy Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    sub = parser.add_subparsers(dest="command", help="Commands")
    
    # start
    p_start = sub.add_parser("start", help="Start the pipeline")
    p_start.add_argument("--live", action="store_true", help="Enable live trading")
    p_start.set_defaults(func=cmd_start)
    
    # once
    p_once = sub.add_parser("once", help="Run single iteration")
    p_once.set_defaults(func=cmd_once)
    
    # status
    p_status = sub.add_parser("status", help="Show system status")
    p_status.set_defaults(func=cmd_status)
    
    # discover
    p_disc = sub.add_parser("discover", help="Discover top wallets")
    p_disc.add_argument("--min-pnl", type=float, default=1_000_000, help="Min all-time PnL ($)")
    p_disc.add_argument("--min-roi", type=float, default=5, help="Min month ROI (%%)")
    p_disc.add_argument("--top-n", type=int, default=10, help="Number of wallets")
    p_disc.add_argument("--save", action="store_true", help="Save to JSON file")
    p_disc.set_defaults(func=cmd_discover)
    
    # test-kucoin
    p_kc = sub.add_parser("test-kucoin", help="Test KuCoin API connection")
    p_kc.set_defaults(func=cmd_test_kucoin)
    
    args = parser.parse_args()
    
    if not args.command:
        # Default: start in paper mode
        args.live = False
        cmd_start(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
