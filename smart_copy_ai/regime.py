"""
Market Regime Detection — Classify current market conditions.
=============================================================
Uses BTC price action, volume, and volatility to determine:
BULL / BEAR / SIDEWAYS / HIGH_VOL

This feeds into the position sizing matrix.
"""

import json
import logging
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from .config import config, MarketRegime

logger = logging.getLogger(__name__)


class RegimeDetector:
    """
    Market regime classifier using BTC as proxy for overall crypto market.
    
    Data sources:
    - KuCoin Futures: BTC klines (candles) for price/volume
    - Hyperliquid: Funding rates as sentiment indicator
    
    Classification logic:
    1. HIGH_VOL → if recent ATR > 2× 30d average ATR (takes priority)
    2. BULL     → BTC 24h change > +5%
    3. BEAR     → BTC 24h change < -5%
    4. SIDEWAYS → Everything else
    """
    
    def __init__(self):
        self.current_regime: MarketRegime = MarketRegime.SIDEWAYS
        self.last_check: float = 0
        self.cfg = config.regime
        self._history: List[Dict] = []  # Recent regime changes
    
    # ─── Public API ──────────────────────────────────────────────────────
    
    def detect(self) -> MarketRegime:
        """
        Run full regime detection. Returns current regime.
        Caches result for check_interval_hours.
        """
        now = time.time()
        cache_secs = self.cfg.check_interval_hours * 3600
        
        if now - self.last_check < cache_secs and self.current_regime:
            return self.current_regime
        
        try:
            # Get BTC candles from KuCoin
            candles = self._fetch_btc_candles()
            
            if not candles or len(candles) < 30:
                logger.warning("Not enough candle data for regime detection")
                return self.current_regime
            
            # Compute indicators
            price_change_24h = self._calc_price_change(candles)
            atr_current, atr_avg = self._calc_atr(candles)
            volume_ratio = self._calc_volume_ratio(candles)
            
            # Classify
            old_regime = self.current_regime
            
            # Priority 1: High volatility
            if atr_current > atr_avg * self.cfg.high_vol_atr_multiplier:
                self.current_regime = MarketRegime.HIGH_VOL
            # Priority 2: Directional
            elif price_change_24h >= self.cfg.bull_threshold_pct:
                self.current_regime = MarketRegime.BULL
            elif price_change_24h <= self.cfg.bear_threshold_pct:
                self.current_regime = MarketRegime.BEAR
            # Priority 3: Sideways
            else:
                self.current_regime = MarketRegime.SIDEWAYS
            
            self.last_check = now
            
            if self.current_regime != old_regime:
                self._history.append({
                    "time": now,
                    "from": old_regime.value,
                    "to": self.current_regime.value,
                    "price_change": price_change_24h,
                    "atr_ratio": atr_current / atr_avg if atr_avg > 0 else 0,
                    "volume_ratio": volume_ratio,
                })
                logger.info(
                    f"🔀 Regime change: {old_regime.value} → {self.current_regime.value} "
                    f"| BTC 24h: {price_change_24h*100:+.2f}% "
                    f"| ATR ratio: {atr_current/atr_avg if atr_avg else 0:.2f}x "
                    f"| Vol ratio: {volume_ratio:.2f}x"
                )
            else:
                logger.debug(
                    f"📊 Regime unchanged: {self.current_regime.value} "
                    f"| BTC 24h: {price_change_24h*100:+.2f}%"
                )
            
            return self.current_regime
            
        except Exception as e:
            logger.error(f"Regime detection error: {e}")
            return self.current_regime
    
    def get_status(self) -> Dict:
        """Get current regime status for monitoring."""
        return {
            "regime": self.current_regime.value,
            "last_check": self.last_check,
            "history": self._history[-10:],
        }
    
    # ─── Data Fetching ───────────────────────────────────────────────────
    
    def _fetch_btc_candles(self, granularity: int = 60, count: int = 720) -> List[List]:
        """
        Fetch BTC/USDT hourly candles from KuCoin Futures.
        
        granularity: minutes per candle (60 = 1h)
        count: number of candles (720 = 30 days of hourly data)
        
        Returns: List of [timestamp, open, high, low, close, volume]
        """
        end_at = int(time.time() * 1000)
        start_at = end_at - (count * granularity * 60 * 1000)
        
        url = (
            f"{config.kucoin.futures_base_url}/api/v1/kline/query"
            f"?symbol=XBTUSDTM&granularity={granularity}"
            f"&from={start_at}&to={end_at}"
        )
        
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                if data.get("code") == "200000":
                    return data.get("data", [])
        except Exception as e:
            logger.warning(f"Failed to fetch BTC candles: {e}")
        
        return []
    
    # ─── Indicators ──────────────────────────────────────────────────────
    
    @staticmethod
    def _calc_price_change(candles: List[List]) -> float:
        """Calculate 24h price change from hourly candles."""
        if len(candles) < 24:
            return 0.0
        
        # KuCoin format: [time, open, high, low, close, volume]
        latest_close = float(candles[-1][4])
        close_24h_ago = float(candles[-24][4])
        
        if close_24h_ago == 0:
            return 0.0
        
        return (latest_close - close_24h_ago) / close_24h_ago
    
    @staticmethod
    def _calc_atr(candles: List[List], period: int = 14) -> Tuple[float, float]:
        """
        Calculate Average True Range.
        Returns: (current ATR, 30-day average ATR)
        """
        if len(candles) < period + 30:
            return 0.0, 1.0
        
        def true_range(i: int) -> float:
            high = float(candles[i][2])
            low = float(candles[i][3])
            prev_close = float(candles[i-1][4])
            return max(high - low, abs(high - prev_close), abs(low - prev_close))
        
        # Compute ATR for each period-length window
        atrs = []
        for end in range(period, len(candles)):
            tr_sum = sum(true_range(i) for i in range(end - period + 1, end + 1))
            atrs.append(tr_sum / period)
        
        if not atrs:
            return 0.0, 1.0
        
        current_atr = atrs[-1]
        # Average ATR over last 30 periods
        avg_window = min(30, len(atrs))
        avg_atr = sum(atrs[-avg_window:]) / avg_window
        
        return current_atr, avg_atr
    
    @staticmethod
    def _calc_volume_ratio(candles: List[List]) -> float:
        """
        Calculate current volume vs 30-day average.
        Returns ratio (> 1 means above average).
        """
        if len(candles) < 30:
            return 1.0
        
        volumes = [float(c[5]) for c in candles if len(c) > 5]
        
        if not volumes or len(volumes) < 30:
            return 1.0
        
        current_vol = sum(volumes[-24:])  # Last 24h
        avg_daily_vol = sum(volumes[-720:]) / 30 if len(volumes) >= 720 else sum(volumes) / (len(volumes) / 24)
        
        if avg_daily_vol == 0:
            return 1.0
        
        return current_vol / avg_daily_vol
    
    # ─── Utility ─────────────────────────────────────────────────────────
    
    def is_high_vol(self) -> bool:
        return self.current_regime == MarketRegime.HIGH_VOL
    
    def is_favorable_for_longs(self) -> bool:
        return self.current_regime in (MarketRegime.BULL, MarketRegime.SIDEWAYS)
    
    def is_favorable_for_shorts(self) -> bool:
        return self.current_regime in (MarketRegime.BEAR, MarketRegime.SIDEWAYS)
    
    def max_positions(self) -> int:
        """Get max open positions for current regime."""
        if self.is_high_vol():
            return config.risk.max_open_positions_high_vol
        return config.risk.max_open_positions_normal
