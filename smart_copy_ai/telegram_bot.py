"""
Telegram Alerts — Trade notifications, daily reports, and emergency alerts.
===========================================================================
Sends formatted messages to a Telegram chat via Bot API.
"""

import json
import logging
import time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, List, Optional

from .config import config
from .signals import Signal, SignalDB

logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Telegram notification bot.
    
    Message types:
    1. Trade opened  → symbol, side, size, entry, SL, TP, ML confidence
    2. Trade closed  → PnL, reason, duration
    3. Signal blocked → reason (ML, CoinGlass veto, circuit breaker)
    4. Daily report   → summary of all trades, PnL, win rate, regime
    5. Emergency      → circuit breaker triggered, large loss
    """
    
    def __init__(self):
        self.token = config.telegram.bot_token
        self.chat_id = config.telegram.chat_id
        self.enabled = config.telegram.enabled and bool(self.token) and bool(self.chat_id)
        
        if not self.enabled:
            logger.info("📵 Telegram alerts disabled (no token/chat_id)")
    
    def _send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram Bot API."""
        if not self.enabled:
            return False
        
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                return result.get("ok", False)
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False
    
    # ─── Trade Notifications ─────────────────────────────────────────────
    
    def notify_trade_opened(self, signal: Signal):
        """Notify when a trade is opened."""
        emoji = "🟢" if signal.side == "LONG" else "🔴"
        paper = "📝 PAPER " if config.pipeline.paper_trading else ""
        
        msg = (
            f"{emoji} <b>{paper}TRADE OPENED</b>\n\n"
            f"<b>Symbol:</b> {signal.symbol}\n"
            f"<b>Side:</b> {signal.side}\n"
            f"<b>Entry:</b> ${signal.fill_price or signal.entry_price:,.2f}\n"
            f"<b>Leverage:</b> {signal.leverage}x\n"
            f"<b>Risk:</b> {signal.final_risk_pct*100:.2f}%\n"
            f"<b>Size:</b> {signal.final_size:.4f}\n\n"
            f"<b>SL:</b> ${signal.sl_price:,.2f} ({signal.sl_pct*100:+.1f}%)\n"
            f"<b>TP:</b> {', '.join(f'${p:,.2f}' for p in signal.tp_prices)}\n\n"
            f"<b>ML:</b> {signal.ml_action} ({signal.ml_confidence:.0%})\n"
            f"<b>Source:</b> {signal.source}\n"
            f"<b>Order ID:</b> <code>{signal.order_id}</code>"
        )
        
        self._send(msg)
    
    def notify_trade_closed(self, signal: Signal):
        """Notify when a trade is closed."""
        pnl = signal.realized_pnl
        emoji = "✅" if pnl >= 0 else "❌"
        duration = ""
        if signal.closed_at and signal.timestamp:
            mins = (signal.closed_at - signal.timestamp) / 60
            if mins < 60:
                duration = f"{mins:.0f}m"
            else:
                duration = f"{mins/60:.1f}h"
        
        msg = (
            f"{emoji} <b>TRADE CLOSED</b>\n\n"
            f"<b>Symbol:</b> {signal.symbol}\n"
            f"<b>Side:</b> {signal.side}\n"
            f"<b>Entry:</b> ${signal.fill_price:,.2f}\n"
            f"<b>Reason:</b> {signal.close_reason}\n"
            f"<b>PnL:</b> ${pnl:+,.2f}\n"
            f"<b>Duration:</b> {duration}\n"
            f"<b>Source:</b> {signal.source}"
        )
        
        self._send(msg)
    
    def notify_signal_blocked(self, signal: Signal, reason: str):
        """Notify when a signal is blocked."""
        msg = (
            f"🚫 <b>SIGNAL BLOCKED</b>\n\n"
            f"<b>Symbol:</b> {signal.symbol}\n"
            f"<b>Side:</b> {signal.side}\n"
            f"<b>Source:</b> {signal.source}\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>ML:</b> {signal.ml_action} ({signal.ml_confidence:.0%})"
        )
        
        self._send(msg)
    
    # ─── Emergency Alerts ────────────────────────────────────────────────
    
    def notify_circuit_breaker(self, daily_loss: float, threshold: float):
        """Alert when circuit breaker is triggered."""
        msg = (
            f"🚨🚨 <b>CIRCUIT BREAKER TRIGGERED</b> 🚨🚨\n\n"
            f"<b>Daily Loss:</b> ${daily_loss:,.2f}\n"
            f"<b>Threshold:</b> {threshold*100:.1f}%\n"
            f"<b>Action:</b> All trading halted for 24 hours\n\n"
            f"⏰ Trading resumes at: "
            f"{datetime.fromtimestamp(time.time() + 86400).strftime('%Y-%m-%d %H:%M')}"
        )
        
        self._send(msg)
    
    def notify_large_loss(self, signal: Signal):
        """Alert on a significant single-trade loss."""
        msg = (
            f"⚠️ <b>LARGE LOSS ALERT</b>\n\n"
            f"<b>Symbol:</b> {signal.symbol}\n"
            f"<b>PnL:</b> ${signal.realized_pnl:+,.2f}\n"
            f"<b>Risk was:</b> {signal.final_risk_pct*100:.2f}%\n"
            f"<b>Source:</b> {signal.source}"
        )
        
        self._send(msg)
    
    # ─── Daily Report ────────────────────────────────────────────────────
    
    def send_daily_report(self, db: SignalDB, regime: str = "UNKNOWN"):
        """Send end-of-day performance summary."""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Get today's closed trades
        trades = db.get_recent_trades(50)
        today_trades = [t for t in trades if t.closed_at and 
                        datetime.fromtimestamp(t.closed_at).strftime("%Y-%m-%d") == today]
        
        total_pnl = sum(t.realized_pnl for t in today_trades)
        wins = sum(1 for t in today_trades if t.realized_pnl > 0)
        losses = sum(1 for t in today_trades if t.realized_pnl <= 0)
        win_rate = wins / len(today_trades) * 100 if today_trades else 0
        
        # Current open positions
        open_positions = db.count_open_positions()
        
        emoji = "📈" if total_pnl >= 0 else "📉"
        
        msg = (
            f"📊 <b>DAILY REPORT — {today}</b>\n"
            f"{'━' * 28}\n\n"
            f"<b>Regime:</b> {regime}\n"
            f"<b>Total Trades:</b> {len(today_trades)}\n"
            f"<b>Win/Loss:</b> {wins}W / {losses}L ({win_rate:.0f}%)\n\n"
            f"{emoji} <b>PnL:</b> ${total_pnl:+,.2f}\n\n"
            f"<b>Open Positions:</b> {open_positions}\n\n"
        )
        
        # Add individual trade list
        if today_trades:
            msg += "<b>Trades:</b>\n"
            for t in today_trades[:10]:
                pnl_emoji = "✅" if t.realized_pnl > 0 else "❌"
                msg += (
                    f"  {pnl_emoji} {t.side} {t.symbol} "
                    f"${t.realized_pnl:+,.2f} ({t.close_reason})\n"
                )
        
        msg += f"\n<i>Paper mode: {'ON' if config.pipeline.paper_trading else 'OFF'}</i>"
        
        self._send(msg)
    
    # ─── Status ──────────────────────────────────────────────────────────
    
    def send_startup_message(self):
        """Send bot startup notification."""
        mode = "📝 PAPER" if config.pipeline.paper_trading else "🔴 LIVE"
        
        msg = (
            f"🤖 <b>Smart Copy AI Started</b>\n\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Wallets tracked:</b> {len(config.wallets.whitelist)}\n"
            f"<b>Max positions:</b> {config.risk.max_open_positions_normal}\n"
            f"<b>Capital:</b> ${config.risk.initial_capital:,.2f}\n"
            f"<b>Loop interval:</b> {config.pipeline.signal_loop_interval_sec}s\n"
            f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        self._send(msg)
    
    def send_status(self, status: Dict):
        """Send current system status."""
        msg = (
            f"📡 <b>System Status</b>\n\n"
            f"<b>Uptime:</b> {status.get('uptime', 'N/A')}\n"
            f"<b>Polls:</b> {status.get('polls', 0)}\n"
            f"<b>Signals today:</b> {status.get('signals_today', 0)}\n"
            f"<b>Trades today:</b> {status.get('trades_today', 0)}\n"
            f"<b>Open positions:</b> {status.get('open_positions', 0)}\n"
            f"<b>Regime:</b> {status.get('regime', 'UNKNOWN')}\n"
        )
        
        self._send(msg)
