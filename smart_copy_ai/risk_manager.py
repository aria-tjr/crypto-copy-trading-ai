"""
Risk Management Engine — Position sizing, daily limits, circuit breaker.
=========================================================================
The guardian that keeps you in the game. Every trade passes through here.
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from .config import config, MarketRegime, MLAction
from .signals import Signal, SignalDB

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Enforces all risk rules before any trade executes.
    
    Checks (in order):
    1. Circuit breaker (is trading paused?)
    2. Daily risk limit
    3. Open position count
    4. Consecutive loss adjustment
    5. Position sizing based on ML action + regime
    6. Leverage cap
    """
    
    def __init__(self, db: SignalDB):
        self.db = db
        self.risk = config.risk
        self.current_capital = self.risk.initial_capital
        self.current_regime = MarketRegime.SIDEWAYS
        self.circuit_breaker_until: Optional[float] = None
        self._daily_pnl_cache = 0.0
        self._daily_risk_cache = 0.0
        self._cache_date = ""
    
    # ─── Capital Tracking ────────────────────────────────────────────────
    
    def update_capital(self, new_capital: float):
        """Update current capital (call after balance check)."""
        self.current_capital = new_capital
        logger.info(f"💰 Capital updated: ${new_capital:,.2f}")
    
    def update_regime(self, regime: MarketRegime):
        """Update current market regime."""
        if regime != self.current_regime:
            logger.info(f"📊 Regime changed: {self.current_regime.value} → {regime.value}")
        self.current_regime = regime
    
    # ─── Main Risk Check ─────────────────────────────────────────────────
    
    def check_trade(self, signal: Signal) -> Tuple[bool, str, float]:
        """
        Main risk gate. Returns (approved, reason, adjusted_risk_pct).
        
        Every signal MUST pass through this before execution.
        """
        
        # 0. Refresh daily stats
        self._refresh_daily_cache()
        
        # 1. Circuit breaker check
        if self.is_circuit_breaker_active():
            remaining = ((self.circuit_breaker_until or 0) - time.time()) / 3600
            return False, f"🔴 Circuit breaker active ({remaining:.1f}h remaining)", 0.0
        
        # 2. ML action check
        ml_action = self._parse_ml_action(signal.ml_action)
        if ml_action == MLAction.BLOCK:
            return False, "🚫 ML filter: BLOCK", 0.0
        
        # 3. CoinGlass veto check
        if signal.coinglass_vetoed:
            return False, "🚫 CoinGlass veto: direction mismatch", 0.0
        
        # 4. Daily risk limit check
        base_risk = self._get_base_risk(ml_action)
        if self._daily_risk_cache + base_risk > self.risk.max_daily_risk_pct:
            return False, f"⚠️ Daily risk limit reached ({self._daily_risk_cache*100:.1f}% / {self.risk.max_daily_risk_pct*100:.0f}%)", 0.0
        
        # 5. Open position count check
        open_count = self.db.count_open_positions()
        max_positions = (
            self.risk.max_open_positions_high_vol 
            if self.current_regime == MarketRegime.HIGH_VOL 
            else self.risk.max_open_positions_normal
        )
        if open_count >= max_positions:
            return False, f"⚠️ Max positions reached ({open_count}/{max_positions})", 0.0
        
        # 6. Check for duplicate symbol
        open_positions = self.db.get_open_positions()
        for pos in open_positions:
            if pos.symbol == signal.symbol and pos.side == signal.side:
                return False, f"⚠️ Already have {signal.side} {signal.symbol} open", 0.0
        
        # 7. Consecutive loss adjustment
        adjusted_risk = self._adjust_for_loss_streak(base_risk)
        
        # 8. Leverage cap
        if signal.leverage > self.risk.max_leverage:
            signal.leverage = self.risk.max_leverage
            logger.warning(f"⚠️ Leverage capped to {self.risk.max_leverage}x")
        
        # All checks passed
        signal.final_risk_pct = adjusted_risk
        return True, "✅ Approved", adjusted_risk
    
    # ─── Position Sizing ─────────────────────────────────────────────────
    
    def calculate_position_size(self, signal: Signal, risk_pct: float) -> float:
        """
        Calculate position size based on risk percentage and stop loss.
        
        Formula: size = (capital × risk_pct) / |entry - stop_loss|
        """
        if risk_pct <= 0:
            return 0.0
        
        risk_amount = self.current_capital * risk_pct
        sl_distance = abs(signal.entry_price * signal.sl_pct)
        
        if sl_distance <= 0:
            logger.warning("⚠️ SL distance is 0, cannot size position")
            return 0.0
        
        # Base size in coins
        size = risk_amount / sl_distance
        
        # Apply leverage
        notional = size * signal.entry_price
        leveraged_margin = notional / signal.leverage
        
        # Ensure we don't exceed capital
        max_margin = self.current_capital * 0.5  # Never use more than 50% as margin
        if leveraged_margin > max_margin:
            size = (max_margin * signal.leverage) / signal.entry_price
            logger.warning(f"⚠️ Position capped to ${max_margin:.2f} margin")
        
        signal.final_size = size
        return size
    
    # ─── Circuit Breaker ─────────────────────────────────────────────────
    
    def check_circuit_breaker(self):
        """Check if daily loss triggers circuit breaker."""
        self._refresh_daily_cache()
        
        daily_loss_pct = self._daily_pnl_cache / self.current_capital if self.current_capital > 0 else 0
        
        if daily_loss_pct < -self.risk.circuit_breaker_daily_loss_pct:
            self.circuit_breaker_until = time.time() + (self.risk.circuit_breaker_cooldown_hours * 3600)
            logger.critical(
                f"🚨 CIRCUIT BREAKER TRIGGERED! Daily loss: {daily_loss_pct*100:.1f}% "
                f"| Trading paused for {self.risk.circuit_breaker_cooldown_hours}h"
            )
            return True
        return False
    
    def is_circuit_breaker_active(self) -> bool:
        """Check if circuit breaker is currently active."""
        if self.circuit_breaker_until is None:
            return False
        if time.time() > self.circuit_breaker_until:
            self.circuit_breaker_until = None
            logger.info("✅ Circuit breaker expired, trading resumed")
            return False
        return True
    
    # ─── Internal Helpers ────────────────────────────────────────────────
    
    def _parse_ml_action(self, action_str: str) -> MLAction:
        """Parse ML action string to enum."""
        try:
            return MLAction(action_str)
        except (ValueError, KeyError):
            return MLAction.BLOCK
    
    def _get_base_risk(self, ml_action: MLAction) -> float:
        """Get base risk percentage from the position sizing matrix."""
        matrix = self.risk.position_size_matrix
        if ml_action in matrix and self.current_regime in matrix[ml_action]:
            return matrix[ml_action][self.current_regime]
        return 0.0
    
    def _adjust_for_loss_streak(self, base_risk: float) -> float:
        """Reduce position size after consecutive losses."""
        streak = self.db.get_consecutive_losses()
        if streak >= self.risk.max_consecutive_losses:
            adjusted = base_risk * self.risk.loss_streak_size_reduction
            logger.warning(
                f"⚠️ Loss streak ({streak}): risk reduced "
                f"{base_risk*100:.2f}% → {adjusted*100:.2f}%"
            )
            return adjusted
        return base_risk
    
    def _refresh_daily_cache(self):
        """Refresh daily PnL and risk cache."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._cache_date != today:
            self._daily_pnl_cache = self.db.get_daily_pnl(today)
            self._daily_risk_cache = self.db.get_daily_risk_used(today)
            self._cache_date = today
    
    # ─── Reporting ───────────────────────────────────────────────────────
    
    def get_risk_status(self) -> dict:
        """Get current risk status summary."""
        self._refresh_daily_cache()
        return {
            "capital": self.current_capital,
            "regime": self.current_regime.value,
            "daily_pnl": self._daily_pnl_cache,
            "daily_pnl_pct": (self._daily_pnl_cache / self.current_capital * 100) if self.current_capital > 0 else 0,
            "daily_risk_used": self._daily_risk_cache,
            "daily_risk_remaining": self.risk.max_daily_risk_pct - self._daily_risk_cache,
            "open_positions": self.db.count_open_positions(),
            "max_positions": (
                self.risk.max_open_positions_high_vol
                if self.current_regime == MarketRegime.HIGH_VOL
                else self.risk.max_open_positions_normal
            ),
            "consecutive_losses": self.db.get_consecutive_losses(),
            "circuit_breaker_active": self.is_circuit_breaker_active(),
        }
