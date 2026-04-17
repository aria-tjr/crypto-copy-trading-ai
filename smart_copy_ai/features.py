"""
Feature Engineering — 35 features across 4 categories for ML model.
====================================================================
Computes features in real-time for each incoming signal.
"""

import json
import logging
import math
import time
import urllib.request
from typing import Dict, List, Optional

from .config import config, MarketRegime
from .signals import Signal, SignalDB
from .coinglass import CoinGlassClient

logger = logging.getLogger(__name__)


class FeatureEngineer:
    """
    Computes 35 features for the XGBoost classifier.
    
    Categories:
    1. Trader Performance (12 features)
    2. Market Regime (8 features)
    3. Signal Quality (10 features)
    4. CoinGlass Preview (5 features)
    """
    
    def __init__(self, db: SignalDB, coinglass: CoinGlassClient):
        self.db = db
        self.coinglass = coinglass
        self._price_cache: Dict[str, Dict] = {}
        self._price_cache_ts: float = 0
    
    # ─── Main Feature Computation ────────────────────────────────────────
    
    def compute_features(self, signal: Signal) -> Dict[str, float]:
        """
        Compute all 35 features for a signal.
        Returns a flat dict of feature_name → float value.
        """
        features = {}
        
        # Category 1: Trader Performance (12 features)
        perf = self._trader_performance_features(signal.wallet_id)
        features.update(perf)
        
        # Category 2: Market Regime (8 features)
        regime = self._market_regime_features(signal.symbol)
        features.update(regime)
        
        # Category 3: Signal Quality (10 features)
        quality = self._signal_quality_features(signal)
        features.update(quality)
        
        # Category 4: CoinGlass Preview (5 features)
        symbol_base = signal.symbol.replace("USDTM", "").replace("USDT", "").replace("USD", "")
        cg = self.coinglass.get_ml_features(symbol_base or "BTC")
        features.update(cg)
        
        # Validate: should have 35 features
        if len(features) != 35:
            logger.warning(f"Expected 35 features, got {len(features)}")
        
        return features
    
    # ─── Category 1: Trader Performance (12 features) ────────────────────
    
    def _trader_performance_features(self, wallet_id: str) -> Dict[str, float]:
        """
        12 features about the source wallet's historical performance.
        """
        recent = self.db.get_recent_trades(50)
        wallet_trades = [t for t in recent if t.wallet_id == wallet_id]
        
        if len(wallet_trades) < 3:
            # Not enough data — return neutral features
            return {
                "trader_avg_win_pct": 0.0,
                "trader_avg_loss_pct": 0.0,
                "trader_win_rate": 0.5,
                "trader_hold_time_p25": 0.0,
                "trader_hold_time_p50": 0.0,
                "trader_hold_time_p75": 0.0,
                "trader_current_win_streak": 0.0,
                "trader_current_loss_streak": 0.0,
                "trader_sharpe_7d": 0.0,
                "trader_profit_factor": 1.0,
                "trader_avg_leverage": 5.0,
                "trader_trade_count_7d": 0.0,
            }
        
        # Calculate stats
        wins = [t for t in wallet_trades if t.realized_pnl > 0]
        losses = [t for t in wallet_trades if t.realized_pnl < 0]
        
        avg_win = sum(t.realized_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.realized_pnl for t in losses) / len(losses) if losses else 0
        win_rate = len(wins) / len(wallet_trades) if wallet_trades else 0.5
        
        # Hold times
        hold_times = []
        for t in wallet_trades:
            if t.closed_at and t.timestamp:
                hold_times.append(t.closed_at - t.timestamp)
        hold_times.sort()
        
        def percentile(data, pct):
            if not data:
                return 0.0
            idx = int(len(data) * pct)
            return data[min(idx, len(data) - 1)]
        
        # Win/loss streaks
        win_streak = 0
        loss_streak = 0
        for t in wallet_trades:
            if t.realized_pnl > 0:
                win_streak += 1
                loss_streak = 0
            else:
                loss_streak += 1
                win_streak = 0
        
        # Sharpe (simplified: mean / std of returns)
        week_ago = time.time() - 7 * 86400
        recent_pnls = [t.realized_pnl for t in wallet_trades if t.timestamp > week_ago]
        if len(recent_pnls) > 1:
            mean_pnl = sum(recent_pnls) / len(recent_pnls)
            variance = sum((p - mean_pnl) ** 2 for p in recent_pnls) / len(recent_pnls)
            std_pnl = math.sqrt(variance) if variance > 0 else 1
            sharpe_7d = mean_pnl / std_pnl
        else:
            sharpe_7d = 0.0
        
        # Profit factor
        gross_profit = sum(t.realized_pnl for t in wins)
        gross_loss = abs(sum(t.realized_pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 2.0
        
        # Average leverage
        avg_lev = sum(t.leverage for t in wallet_trades) / len(wallet_trades)
        
        # Trade count in last 7 days
        trades_7d = sum(1 for t in wallet_trades if t.timestamp > week_ago)
        
        return {
            "trader_avg_win_pct": avg_win,
            "trader_avg_loss_pct": avg_loss,
            "trader_win_rate": win_rate,
            "trader_hold_time_p25": percentile(hold_times, 0.25),
            "trader_hold_time_p50": percentile(hold_times, 0.50),
            "trader_hold_time_p75": percentile(hold_times, 0.75),
            "trader_current_win_streak": float(win_streak),
            "trader_current_loss_streak": float(loss_streak),
            "trader_sharpe_7d": sharpe_7d,
            "trader_profit_factor": min(profit_factor, 10.0),  # Cap at 10
            "trader_avg_leverage": avg_lev,
            "trader_trade_count_7d": float(trades_7d),
        }
    
    # ─── Category 2: Market Regime (8 features) ─────────────────────────
    
    def _market_regime_features(self, symbol: str) -> Dict[str, float]:
        """
        8 features about current market conditions.
        Uses KuCoin public API for price data.
        """
        # Default values if API fails
        defaults = {
            "market_btc_change_24h": 0.0,
            "market_bull_score": 0.0,
            "market_bear_score": 0.0,
            "market_sideways_score": 1.0,
            "market_volatility_regime": 0.5,
            "market_funding_trend": 0.0,
            "market_oi_change_1h": 0.0,
            "market_oi_change_4h": 0.0,
        }
        
        try:
            # BTC 24h ticker from KuCoin
            url = f"{config.kucoin.futures_base_url}/api/v1/ticker?symbol=XBTUSDTM"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            
            if data.get("code") == "200000":
                ticker = data.get("data", {})
                price = float(ticker.get("price", 0))
                price_change = float(ticker.get("priceChgPct", 0))
                vol_24h = float(ticker.get("vol24h", 0))
                
                # Regime classification
                bull_score = max(0, price_change / 0.05)  # Normalized to bull threshold
                bear_score = max(0, -price_change / 0.05)
                sideways_score = max(0, 1 - abs(price_change) / 0.02)
                
                # Normalize scores to sum to 1
                total = bull_score + bear_score + sideways_score
                if total > 0:
                    bull_score /= total
                    bear_score /= total
                    sideways_score /= total
                
                defaults.update({
                    "market_btc_change_24h": price_change,
                    "market_bull_score": bull_score,
                    "market_bear_score": bear_score,
                    "market_sideways_score": sideways_score,
                    "market_volatility_regime": min(abs(price_change) / 0.03, 1.0),
                })
        except Exception as e:
            logger.warning(f"Failed to fetch market data: {e}")
        
        # CoinGlass OI data
        try:
            oi = self.coinglass.get_oi_change("BTC")
            if oi:
                defaults["market_oi_change_1h"] = oi["change_1h"]
                defaults["market_oi_change_4h"] = oi["change_4h"]
            
            funding = self.coinglass.get_funding_rate("BTC")
            if funding is not None:
                defaults["market_funding_trend"] = funding
        except Exception:
            pass
        
        return defaults
    
    # ─── Category 3: Signal Quality (10 features) ───────────────────────
    
    def _signal_quality_features(self, signal: Signal) -> Dict[str, float]:
        """
        10 features about the quality of the specific signal.
        """
        defaults = {
            "signal_distance_sma20": 0.0,
            "signal_distance_sma50": 0.0,
            "signal_distance_sma200": 0.0,
            "signal_rsi": 50.0,
            "signal_rsi_zone": 0.0,  # -1 oversold, 0 normal, 1 overbought
            "signal_volume_ratio": 1.0,
            "signal_implied_move_pct": 0.0,
            "signal_sl_distance": abs(signal.sl_pct),
            "signal_tp_distance": signal.tp_pcts[0] if signal.tp_pcts else 0.03,
            "signal_risk_reward": 0.0,
        }
        
        # Risk/reward ratio
        if abs(signal.sl_pct) > 0 and signal.tp_pcts:
            defaults["signal_risk_reward"] = signal.tp_pcts[0] / abs(signal.sl_pct)
        
        # Try to fetch technical data from KuCoin klines
        try:
            symbol = signal.symbol or "XBTUSDTM"
            url = (
                f"{config.kucoin.futures_base_url}/api/v1/kline/query"
                f"?symbol={symbol}&granularity=60&from={int(time.time()) - 14400}"  # Last 4h of 1min
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            
            if data.get("code") == "200000" and data.get("data"):
                klines = data["data"]  # [[time, open, high, low, close, volume], ...]
                closes = [float(k[4]) for k in klines if len(k) >= 5]
                volumes = [float(k[5]) for k in klines if len(k) >= 6]
                
                if len(closes) >= 20:
                    current = closes[-1]
                    sma20 = sum(closes[-20:]) / 20
                    defaults["signal_distance_sma20"] = (current - sma20) / sma20
                    
                    if len(closes) >= 50:
                        sma50 = sum(closes[-50:]) / 50
                        defaults["signal_distance_sma50"] = (current - sma50) / sma50
                    
                    if len(closes) >= 200:
                        sma200 = sum(closes[-200:]) / 200
                        defaults["signal_distance_sma200"] = (current - sma200) / sma200
                    
                    # RSI (14-period)
                    rsi = self._compute_rsi(closes, 14)
                    defaults["signal_rsi"] = rsi
                    if rsi < 30:
                        defaults["signal_rsi_zone"] = -1.0
                    elif rsi > 70:
                        defaults["signal_rsi_zone"] = 1.0
                    else:
                        defaults["signal_rsi_zone"] = 0.0
                
                if volumes and len(volumes) >= 24:
                    avg_vol = sum(volumes[-24:]) / 24
                    current_vol = volumes[-1]
                    defaults["signal_volume_ratio"] = current_vol / avg_vol if avg_vol > 0 else 1.0
                
                # Implied move from recent ATR
                if len(closes) >= 14:
                    highs = [float(k[2]) for k in klines[-14:] if len(k) >= 3]
                    lows = [float(k[3]) for k in klines[-14:] if len(k) >= 4]
                    if highs and lows and closes:
                        atr = sum(h - l for h, l in zip(highs, lows)) / len(highs)
                        defaults["signal_implied_move_pct"] = atr / closes[-1] if closes[-1] > 0 else 0
        
        except Exception as e:
            logger.debug(f"Failed to compute signal quality features: {e}")
        
        return defaults
    
    # ─── Helpers ─────────────────────────────────────────────────────────
    
    @staticmethod
    def _compute_rsi(prices: List[float], period: int = 14) -> float:
        """Compute RSI from price series."""
        if len(prices) < period + 1:
            return 50.0
        
        gains = []
        losses = []
        for i in range(1, len(prices)):
            change = prices[i] - prices[i - 1]
            if change >= 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        # Wilder's smoothing
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    # ─── Regime Detection ────────────────────────────────────────────────
    
    def detect_regime(self) -> MarketRegime:
        """
        Classify current market regime.
        Called every 24h to update model variant.
        """
        features = self._market_regime_features("XBTUSDTM")
        
        btc_change = features["market_btc_change_24h"]
        vol = features["market_volatility_regime"]
        
        # High volatility overrides everything
        if vol > 0.8:
            return MarketRegime.HIGH_VOL
        
        if btc_change > config.regime.bull_threshold_pct:
            return MarketRegime.BULL
        elif btc_change < config.regime.bear_threshold_pct:
            return MarketRegime.BEAR
        else:
            return MarketRegime.SIDEWAYS
