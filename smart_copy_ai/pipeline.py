"""
Pipeline Orchestrator — Main 30-second loop that ties everything together.
==========================================================================
Flow (every 30 seconds):
  1. Poll Hyperliquid wallets → detect new positions
  2. For each new signal:
     a. Compute 35 features  (features.py)
     b. ML filter → FULL / REDUCE_75 / REDUCE_50 / BLOCK  (ml_filter.py)
     c. CoinGlass veto check  (coinglass.py)
     d. Position sizing  (risk_manager.py)
     e. Execute on KuCoin  (executor.py)
  3. Check existing positions → trailing SL, TP hits
  4. Every 24h → regime detection, daily report
"""

import logging
import time
import traceback
from datetime import datetime
from typing import Dict, List, Optional

from .config import config, MarketRegime, MLAction
from .signals import Signal, SignalDB, WhitelistValidator
from .wallet_monitor import WalletMonitor
from .features import FeatureEngineer
from .ml_filter import MLFilter
from .coinglass import CoinGlassClient
from .risk_manager import RiskManager
from .executor import KuCoinExecutor
from .regime import RegimeDetector
from .telegram_bot import TelegramBot

logger = logging.getLogger(__name__)


class Pipeline:
    """
    Main pipeline orchestrator.
    
    Responsibilities:
    - Run the 30-second signal detection loop
    - Coordinate all modules in the correct order
    - Track system state and uptime
    - Handle errors gracefully (never crash the loop)
    """
    
    def __init__(self):
        # Core components
        self.db = SignalDB()
        self.whitelist = WhitelistValidator()
        self.monitor = WalletMonitor()
        self.coinglass = CoinGlassClient()
        self.features = FeatureEngineer(self.db, self.coinglass)
        self.ml = MLFilter(self.db)
        self.risk = RiskManager(self.db)
        self.executor = KuCoinExecutor(self.db)
        self.regime = RegimeDetector()
        self.telegram = TelegramBot()
        
        # State
        self.running = False
        self.start_time: float = 0
        self.loop_count: int = 0
        self.signals_processed: int = 0
        self.trades_executed: int = 0
        self.last_daily_report: str = ""
        
    # ─── Main Loop ───────────────────────────────────────────────────────
    
    def start(self):
        """Start the main pipeline loop."""
        self.running = True
        self.start_time = time.time()
        
        logger.info("=" * 60)
        logger.info("🚀 SMART COPY AI — Starting pipeline")
        logger.info(f"   Mode: {'PAPER' if config.pipeline.paper_trading else 'LIVE'}")
        logger.info(f"   Wallets: {len(self.monitor.wallets)}")
        logger.info(f"   Interval: {config.pipeline.signal_loop_interval_sec}s")
        logger.info(f"   Capital: ${config.risk.initial_capital:,.2f}")
        logger.info("=" * 60)
        
        # Startup notification
        self.telegram.send_startup_message()
        
        # Initial regime detection
        current_regime = self.regime.detect()
        logger.info(f"📊 Initial regime: {current_regime.value}")
        
        # Main loop
        while self.running:
            try:
                self._loop_iteration()
            except KeyboardInterrupt:
                logger.info("⏹️ Shutting down (keyboard interrupt)")
                self.running = False
            except Exception as e:
                logger.error(f"🔥 Loop error (continuing): {e}\n{traceback.format_exc()}")
                time.sleep(5)  # Brief pause on error
            
            time.sleep(config.pipeline.signal_loop_interval_sec)
        
        logger.info("🛑 Pipeline stopped")
    
    def stop(self):
        """Gracefully stop the pipeline."""
        self.running = False
    
    def _loop_iteration(self):
        """Single iteration of the main loop."""
        self.loop_count += 1
        
        # ── Step 0: Periodic checks ──────────────────────────────────
        self._periodic_checks()
        
        # ── Step 1: Check circuit breaker ────────────────────────────
        if self.risk.check_circuit_breaker():
            if self.loop_count % 60 == 1:  # Log once per ~30 min
                logger.info("🛑 Circuit breaker active — skipping")
            return
        
        # ── Step 2: Poll wallets for new signals ─────────────────────
        raw_signals = self.monitor.poll_all()
        
        if not raw_signals:
            return  # Nothing new
        
        logger.info(f"📡 {len(raw_signals)} new signal(s) detected")
        
        # ── Step 3: Process each signal through the pipeline ─────────
        for signal in raw_signals:
            self._process_signal(signal)
        
        # ── Step 4: Check existing positions ─────────────────────────
        self.executor.check_trailing_stop()
    
    def _process_signal(self, signal: Signal):
        """
        Process a single signal through all pipeline layers.
        
        Flow: Signal → Whitelist → Features → ML → CoinGlass → Risk → Execute
        """
        self.signals_processed += 1
        logger.info(f"\n{'─'*50}")
        logger.info(f"🔄 Processing: {signal.summary()}")
        
        # ── Layer 0: Whitelist check ─────────────────────────────────
        # Skip whitelist if no wallets configured yet (early development)
        if config.wallets.whitelist and not self.whitelist.validate_signal(signal):
            logger.info(f"🚫 Blocked: wallet not whitelisted ({signal.wallet_id[:10]}...)")
            self.db.save_signal(signal)
            return
        
        # ── Layer 1: Compute features ────────────────────────────────
        try:
            feature_vector = self.features.compute_features(signal)
        except Exception as e:
            logger.warning(f"Feature computation failed: {e}")
            feature_vector = None
        
        # ── Layer 2: ML Filter (20% weight) ──────────────────────────
        current_regime = self.regime.current_regime
        
        if feature_vector and not self.ml.passthrough:
            ml_action, ml_confidence = self.ml.predict(feature_vector)
            signal.ml_action = ml_action
            signal.ml_confidence = ml_confidence
            
            logger.info(f"🤖 ML: {ml_action} ({ml_confidence:.0%})")
            
            if ml_action == "BLOCK":
                signal.status = "BLOCKED"
                signal.close_reason = f"ML_BLOCK ({ml_confidence:.0%})"
                self.db.save_signal(signal)
                self.telegram.notify_signal_blocked(signal, f"ML: {ml_action}")
                logger.info(f"🚫 Blocked by ML filter")
                return
        else:
            # ML not ready → default to FULL (Layer 1 raw copy at 80%)
            signal.ml_action = "FULL"
            signal.ml_confidence = 0.5
            logger.info("🤖 ML not ready → defaulting to FULL")
        
        # ── Layer 3: CoinGlass Veto ──────────────────────────────────
        try:
            vetoed, veto_reason, cg_boost = self.coinglass.check_veto(signal)
            signal.coinglass_vetoed = vetoed
            
            if vetoed:
                signal.status = "BLOCKED"
                signal.close_reason = f"COINGLASS_VETO: {veto_reason}"
                self.db.save_signal(signal)
                self.telegram.notify_signal_blocked(signal, f"CoinGlass: {veto_reason}")
                logger.info(f"🚫 CoinGlass veto: {veto_reason}")
                return
            
            # Confidence boost from CoinGlass
            if cg_boost > 1.0:
                signal.ml_confidence = min(1.0, signal.ml_confidence * cg_boost)
                logger.info(f"📈 CoinGlass confidence boost: ×{cg_boost:.2f}")
                
        except Exception as e:
            logger.warning(f"CoinGlass check failed (continuing): {e}")
        
        # ── Layer 4: Full risk check + position sizing ───────────────
        self.risk.update_regime(current_regime)
        approved, reason, risk_pct = self.risk.check_trade(signal)
        
        if not approved:
            signal.status = "BLOCKED"
            signal.close_reason = reason
            self.db.save_signal(signal)
            self.telegram.notify_signal_blocked(signal, reason)
            logger.info(f"🚫 Risk block: {reason}")
            return
        
        signal.final_risk_pct = risk_pct
        
        # Convert risk % to position size
        capital = self.executor.get_current_capital()
        size = self.risk.calculate_position_size(signal, risk_pct)
        
        if size <= 0 or risk_pct <= 0:
            signal.status = "BLOCKED"
            signal.close_reason = "zero_size"
            self.db.save_signal(signal)
            logger.info("🚫 Zero position size — skipping")
            return
        
        signal.status = "APPROVED"
        logger.info(
            f"✅ APPROVED: risk={risk_pct*100:.3f}% | "
            f"size={signal.final_size:.6f} | capital=${capital:,.2f}"
        )
        
        # ── Layer 5: Execute ─────────────────────────────────────────
        success, result = self.executor.open_position(signal)
        
        if success:
            self.trades_executed += 1
            self.telegram.notify_trade_opened(signal)
            logger.info(f"🎯 EXECUTED: {result}")
        else:
            signal.status = "BLOCKED"
            signal.close_reason = f"execution_failed: {result}"
            self.db.save_signal(signal)
            logger.error(f"❌ Execution failed: {result}")
    
    # ─── Periodic Tasks ──────────────────────────────────────────────────
    
    def _periodic_checks(self):
        """Run periodic tasks (regime detection, daily report)."""
        now = datetime.now()
        
        # Regime detection (every check_interval_hours)
        if self.loop_count % (config.regime.check_interval_hours * 120) == 0:
            self.regime.detect()
        
        # Daily report at 23:55
        today = now.strftime("%Y-%m-%d")
        if now.hour == 23 and now.minute >= 55 and self.last_daily_report != today:
            self.last_daily_report = today
            self.telegram.send_daily_report(self.db, self.regime.current_regime.value)
            logger.info("📊 Daily report sent")
    
    # ─── Status ──────────────────────────────────────────────────────────
    
    def get_status(self) -> Dict:
        """Get comprehensive pipeline status."""
        uptime_secs = time.time() - self.start_time if self.start_time else 0
        hours = int(uptime_secs // 3600)
        mins = int((uptime_secs % 3600) // 60)
        
        return {
            "running": self.running,
            "uptime": f"{hours}h {mins}m",
            "loop_count": self.loop_count,
            "signals_processed": self.signals_processed,
            "trades_executed": self.trades_executed,
            "open_positions": self.db.count_open_positions(),
            "regime": self.regime.current_regime.value,
            "circuit_breaker": self.risk.check_circuit_breaker(),
            "paper_mode": config.pipeline.paper_trading,
            "wallets": self.monitor.get_status(),
        }
    
    def run_once(self) -> Dict:
        """Run a single pipeline iteration (for testing)."""
        self.start_time = self.start_time or time.time()
        self._loop_iteration()
        return self.get_status()
