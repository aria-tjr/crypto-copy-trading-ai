"""
CoinGlass Integration — Layer 3 hard veto + sentiment validation.
=================================================================
Has 100% veto power over all signals. Fetches top trader ratios,
funding rates, and open interest data.
"""

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Dict, Optional, Tuple

from .config import config, SignalSide
from .signals import Signal

logger = logging.getLogger(__name__)


class CoinGlassClient:
    """
    CoinGlass API client for market sentiment data.
    
    Layer 3 — Hard veto conditions:
    - LONG signal → BLOCK if LS ratio < 0.85 AND funding > +0.05%
    - SHORT signal → BLOCK if LS ratio > 1.15 AND funding < -0.05%
    """
    
    def __init__(self):
        self.cfg = config.coinglass
        self.veto_cfg = config.coinglass_veto
        self._cache: Dict[str, Dict] = {}
        self._cache_ts: Dict[str, float] = {}
        self._cache_ttl = 60  # Cache for 60 seconds
    
    # ─── Symbol Mapping ────────────────────────────────────────────────
    
    # Hyperliquid uses non-standard symbols for some tokens
    SYMBOL_MAP = {
        "XBT": "BTC",       # BitMEX-style Bitcoin
        "KFLOKI": "FLOKI",  # Hyperliquid k-prefix
        "KPEPE": "PEPE",
        "KSHIB": "SHIB",
        "KBONK": "BONK",
        "KLUNC": "LUNC",
        "KBTT": "BTT",
        "KDOGE": "DOGE",
        "KWIF": "WIF",
        "PURR": None,        # Hyperliquid-native, no CoinGlass equivalent
        "HFUN": None,
        "JEFF": None,
    }
    
    def _normalize_symbol(self, symbol: str) -> Optional[str]:
        """
        Normalize symbol from Hyperliquid/KuCoin format to CoinGlass format.
        
        Handles:
        - XBT → BTC (BitMEX naming)
        - kFLOKI/KFLOKI → FLOKI (Hyperliquid k-prefix for sub-penny tokens)
        - Standard symbols pass through unchanged
        - Returns None for Hyperliquid-native tokens with no CoinGlass data
        """
        sym = symbol.upper().strip()
        
        # Direct mapping first
        if sym in self.SYMBOL_MAP:
            return self.SYMBOL_MAP[sym]
        
        # Hyperliquid k-prefix pattern: strip leading K for known patterns
        if sym.startswith("K") and len(sym) > 2:
            stripped = sym[1:]
            # Only strip if the remainder is a known token (avoid stripping KAVA→AVA etc.)
            known_k_tokens = {"FLOKI", "PEPE", "SHIB", "BONK", "LUNC", "BTT", "DOGE", "WIF"}
            if stripped in known_k_tokens:
                return stripped
        
        return sym
    
    # ─── API Calls ───────────────────────────────────────────────────────
    
    def _request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make an authenticated request to CoinGlass API."""
        if not self.cfg.api_key:
            logger.warning("⚠️ CoinGlass API key not set — veto layer disabled")
            return None
        
        url = f"{self.cfg.base_url}{endpoint}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        
        headers = {
            "accept": "application/json",
            "CG-API-KEY": self.cfg.api_key,
        }
        
        req = urllib.request.Request(url, headers=headers)
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get("code") == "0" or data.get("success"):
                    return data.get("data")
                logger.warning(f"CoinGlass API error: {data}")
                return None
        except Exception as e:
            logger.error(f"CoinGlass request failed: {e}")
            return None
    
    def _cached_request(self, key: str, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Request with caching."""
        now = time.time()
        if key in self._cache and (now - self._cache_ts.get(key, 0)) < self._cache_ttl:
            return self._cache[key]
        
        data = self._request(endpoint, params)
        if data is not None:
            self._cache[key] = data
            self._cache_ts[key] = now
        return data
    
    # ─── Data Fetchers ───────────────────────────────────────────────────
    
    def _symbol_to_pair(self, symbol: str) -> str:
        """Convert base symbol (BTC) to pair format (BTCUSDT) for L/S ratio endpoints."""
        symbol = symbol.upper()
        if symbol.endswith("USDT"):
            return symbol
        return f"{symbol}USDT"
    
    def get_top_trader_ls_ratio(self, symbol: str = "BTC", period: str = "4h") -> Optional[float]:
        """
        Get top trader long/short ratio from Binance.
        
        v4 endpoint: /api/futures/top-long-short-account-ratio/history
        Requires pair symbol (BTCUSDT), exchange, and interval.
        Hobbyist plan supports: 4h, 6h, 8h, 12h, 1d, 1w (NOT h1).
        
        Returns: ratio > 1 means more longs, < 1 means more shorts.
        """
        pair = self._symbol_to_pair(symbol)
        # Clamp period to Hobbyist-supported intervals
        supported = {"4h", "6h", "8h", "12h", "1d", "1w"}
        if period not in supported:
            period = "4h"
        
        cache_key = f"ls_ratio_{pair}_{period}"
        data = self._cached_request(
            cache_key,
            "/api/futures/top-long-short-account-ratio/history",
            {"symbol": pair, "interval": period, "exchange": "Binance"}
        )
        
        if data and isinstance(data, list) and len(data) > 0:
            latest = data[-1]
            if isinstance(latest, dict):
                return float(latest.get("top_account_long_short_ratio", 1.0))
        
        return None
    
    def get_global_ls_ratio(self, symbol: str = "BTC", period: str = "4h") -> Optional[float]:
        """
        Get global long/short account ratio (all traders, not just top).
        Useful as a backup/secondary signal.
        """
        pair = self._symbol_to_pair(symbol)
        supported = {"4h", "6h", "8h", "12h", "1d", "1w"}
        if period not in supported:
            period = "4h"
        
        cache_key = f"global_ls_{pair}_{period}"
        data = self._cached_request(
            cache_key,
            "/api/futures/global-long-short-account-ratio/history",
            {"symbol": pair, "interval": period, "exchange": "Binance"}
        )
        
        if data and isinstance(data, list) and len(data) > 0:
            latest = data[-1]
            if isinstance(latest, dict):
                return float(latest.get("global_account_long_short_ratio", 1.0))
        
        return None
    
    def get_funding_rate(self, symbol: str = "BTC") -> Optional[float]:
        """
        Get current average funding rate across major exchanges.
        
        v4 endpoint: /api/futures/funding-rate/exchange-list
        Uses base symbol (BTC, ETH, etc.).
        Returns funding rate as a decimal (e.g., 0.0001 = 0.01%).
        """
        cache_key = f"funding_{symbol}"
        data = self._cached_request(
            cache_key,
            "/api/futures/funding-rate/exchange-list",
            {"symbol": symbol.upper()}
        )
        
        if data and isinstance(data, list) and len(data) > 0:
            item = data[0]
            if isinstance(item, dict):
                margins = item.get("stablecoin_margin_list", [])
                if margins:
                    # Average funding rate across top exchanges
                    rates = []
                    for m in margins:
                        if isinstance(m, dict) and m.get("funding_rate") is not None:
                            rates.append(float(m["funding_rate"]))
                    if rates:
                        # Return average, already as percentage fraction
                        # e.g., 0.003745 = 0.3745% per interval
                        # Normalize to per-8h rate for consistency
                        avg = sum(rates) / len(rates)
                        return avg / 100.0  # Convert from percentage to decimal
        
        return None
    
    def get_oi_change(self, symbol: str = "BTC") -> Optional[Dict]:
        """
        Get open interest data (aggregated across all exchanges).
        
        v4 endpoint: /api/futures/open-interest/exchange-list
        Uses base symbol (BTC, ETH, etc.).
        First entry with exchange="All" has aggregated data.
        """
        cache_key = f"oi_{symbol}"
        data = self._cached_request(
            cache_key,
            "/api/futures/open-interest/exchange-list",
            {"symbol": symbol.upper()}
        )
        
        if data and isinstance(data, list):
            # Find the "All" (aggregated) entry
            for item in data:
                if isinstance(item, dict) and item.get("exchange") == "All":
                    return {
                        "current": float(item.get("open_interest_usd", 0)),
                        "change_1h": float(item.get("open_interest_change_percent_1h", 0)) / 100.0,
                        "change_4h": float(item.get("open_interest_change_percent_4h", 0)) / 100.0,
                        "change_24h": float(item.get("open_interest_change_percent_24h", 0)) / 100.0,
                    }
        
        return None
    
    # ─── Veto Logic ──────────────────────────────────────────────────────
    
    def check_veto(self, signal: Signal) -> Tuple[bool, str, float]:
        """
        Layer 3 — Hard veto check.
        
        Returns: (vetoed: bool, reason: str, confidence_multiplier: float)
        """
        # Extract base symbol from trading pair (e.g., "BTCUSDTM" → "BTC")
        symbol = signal.symbol.replace("USDTM", "").replace("USDT", "").replace("USD", "")
        if not symbol:
            symbol = "BTC"
        
        # Normalize for CoinGlass (XBT→BTC, KFLOKI→FLOKI, etc.)
        symbol = self._normalize_symbol(symbol)
        if symbol is None:
            logger.info(f"ℹ️ {signal.symbol} is Hyperliquid-native — no CoinGlass data, skipping veto")
            return False, "hyperliquid_native_token", 1.0
        
        # Fetch data (4h is minimum interval on Hobbyist plan)
        ls_ratio = self.get_top_trader_ls_ratio(symbol, "4h")
        funding = self.get_funding_rate(symbol)
        
        # If we can't get data, don't veto (fail open)
        if ls_ratio is None or funding is None:
            logger.warning(f"⚠️ CoinGlass data unavailable for {symbol} — skipping veto")
            return False, "data_unavailable", 1.0
        
        logger.info(f"📊 CoinGlass {symbol}: LS ratio={ls_ratio:.3f}, funding={funding*100:.4f}%")
        
        confidence_multiplier = 1.0
        
        # LONG signal veto check
        if signal.side == "LONG":
            if (ls_ratio < self.veto_cfg.long_block_ratio_threshold and 
                funding > self.veto_cfg.long_block_funding_threshold):
                reason = (
                    f"LS ratio {ls_ratio:.2f} < {self.veto_cfg.long_block_ratio_threshold} "
                    f"AND funding {funding*100:.4f}% > +0.05% (crowd already long)"
                )
                logger.warning(f"🚫 VETO LONG {symbol}: {reason}")
                return True, reason, 0.0
            
            # Confidence boost for strong directional extreme
            if ls_ratio > self.veto_cfg.strong_long_ratio:
                confidence_multiplier = self.veto_cfg.confidence_boost
                logger.info(f"📈 Confidence boost ×{confidence_multiplier} (strong long ratio {ls_ratio:.2f})")
        
        # SHORT signal veto check
        elif signal.side == "SHORT":
            if (ls_ratio > self.veto_cfg.short_block_ratio_threshold and 
                funding < self.veto_cfg.short_block_funding_threshold):
                reason = (
                    f"LS ratio {ls_ratio:.2f} > {self.veto_cfg.short_block_ratio_threshold} "
                    f"AND funding {funding*100:.4f}% < -0.05% (crowd already short)"
                )
                logger.warning(f"🚫 VETO SHORT {symbol}: {reason}")
                return True, reason, 0.0
            
            # Confidence boost
            if ls_ratio < self.veto_cfg.strong_short_ratio:
                confidence_multiplier = self.veto_cfg.confidence_boost
                logger.info(f"📈 Confidence boost ×{confidence_multiplier} (strong short ratio {ls_ratio:.2f})")
        
        return False, "passed", confidence_multiplier
    
    # ─── Feature Extraction (for ML) ────────────────────────────────────
    
    def get_ml_features(self, symbol: str = "BTC") -> Dict[str, float]:
        """
        Extract CoinGlass features for the ML model (5 features).
        
        Features (adapted for Hobbyist plan — minimum 4h interval):
        - cg_ls_ratio_4h     — top trader L/S ratio at 4h
        - cg_ls_ratio_1d     — top trader L/S ratio at 1d (trend)
        - cg_funding_rate    — avg funding rate across exchanges
        - cg_oi_change_1h    — 1h OI change (from exchange-list)
        - cg_funding_oi_composite — funding × OI direction
        """
        # Normalize symbol for CoinGlass
        normalized = self._normalize_symbol(symbol)
        if normalized is None:
            # Hyperliquid-native token — return neutral defaults
            return {
                "cg_ls_ratio_4h": 1.0,
                "cg_ls_ratio_1d": 1.0,
                "cg_funding_rate": 0.0,
                "cg_oi_change_1h": 0.0,
                "cg_funding_oi_composite": 0.0,
            }
        symbol = normalized
        
        ls_4h = self.get_top_trader_ls_ratio(symbol, "4h") or 1.0
        ls_1d = self.get_top_trader_ls_ratio(symbol, "1d") or 1.0
        funding = self.get_funding_rate(symbol) or 0.0
        oi = self.get_oi_change(symbol)
        oi_1h = oi["change_1h"] if oi else 0.0
        
        # Composite: funding × OI direction
        funding_oi_composite = funding * (1 + oi_1h) if oi else funding
        
        return {
            "cg_ls_ratio_4h": ls_4h,
            "cg_ls_ratio_1d": ls_1d,
            "cg_funding_rate": funding,
            "cg_oi_change_1h": oi_1h,
            "cg_funding_oi_composite": funding_oi_composite,
        }
