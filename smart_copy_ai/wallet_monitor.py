"""
Hyperliquid Wallet Monitor — Real-time position change detection.
=================================================================
Polls the Hyperliquid clearinghouse for whitelisted wallets every 30 seconds.
Detects new/closed/changed positions and emits Signals.
"""

import json
import logging
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .config import config
from .signals import Signal

logger = logging.getLogger(__name__)

# ─── Hyperliquid API ─────────────────────────────────────────────────────

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def _hl_post(payload: dict, timeout: int = 10) -> Optional[dict]:
    """POST to Hyperliquid info API."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_INFO_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"HL API error: {e}")
        return None


@dataclass
class WalletPosition:
    """Snapshot of a single position from Hyperliquid."""
    symbol: str
    side: str           # "LONG" or "SHORT"
    size: float
    entry_px: float
    leverage: float
    unrealized_pnl: float
    margin_used: float
    liquidation_px: float = 0.0

    @property
    def notional(self) -> float:
        return abs(self.size * self.entry_px)


@dataclass
class WalletSnapshot:
    """Full snapshot of a wallet's positions."""
    address: str
    timestamp: float
    account_value: float
    positions: Dict[str, WalletPosition] = field(default_factory=dict)


class WalletMonitor:
    """
    Monitors Hyperliquid wallets for position changes.
    
    Flow:
    1. Poll each whitelisted address every ~30s
    2. Compare to previous snapshot
    3. Detect: NEW positions, CLOSED positions, SIZE changes
    4. Emit Signal objects for new/increased positions
    """
    
    def __init__(self):
        self.wallets: List[str] = list(config.wallets.whitelist.keys())
        self.aliases: Dict[str, str] = config.wallets.aliases.copy()
        self._snapshots: Dict[str, WalletSnapshot] = {}  # address → last snapshot
        self._poll_count: int = 0
        
        logger.info(f"🔍 Wallet Monitor: tracking {len(self.wallets)} addresses")
    
    # ─── Leaderboard Data ────────────────────────────────────────────────
    
    @staticmethod
    def fetch_leaderboard() -> List[Dict]:
        """Fetch Hyperliquid leaderboard for wallet discovery."""
        req = urllib.request.Request(
            HL_LEADERBOARD_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, list):
                    return data
                return data.get("leaderboardRows", data.get("rows", []))
        except Exception as e:
            logger.error(f"Leaderboard fetch failed: {e}")
            return []
    
    @staticmethod
    def discover_top_wallets(
        min_all_time_pnl: float = 1_000_000,
        min_month_roi: float = 0.05,
        top_n: int = 10
    ) -> List[Dict]:
        """
        Auto-discover top performers from leaderboard.
        
        Filters: 
        - All-time PnL >= min_all_time_pnl
        - Month ROI >= min_month_roi (5%)
        - Account value >= $100K (not a tiny account)
        """
        rows = WalletMonitor.fetch_leaderboard()
        if not rows:
            return []
        
        qualified = []
        for row in rows:
            addr = row.get("ethAddress", "")
            perf = row.get("windowPerformances", [])
            name = row.get("displayName", addr[:10])
            acct = float(row.get("accountValue", 0))
            
            if acct < 100_000:
                continue
            
            # Parse window performances: [[timeframe, {pnl, roi, vlm}], ...]
            alltime_pnl = 0
            month_roi = 0
            week_pnl = 0
            
            for window in perf:
                if len(window) < 2:
                    continue
                tf, metrics = window[0], window[1]
                pnl = float(metrics.get("pnl", 0))
                roi = float(metrics.get("roi", 0))
                
                if tf == "allTime":
                    alltime_pnl = pnl
                elif tf == "month":
                    month_roi = roi
                elif tf == "week":
                    week_pnl = pnl
            
            if alltime_pnl >= min_all_time_pnl and month_roi >= min_month_roi:
                qualified.append({
                    "address": addr,
                    "name": name,
                    "account_value": acct,
                    "alltime_pnl": alltime_pnl,
                    "month_roi": month_roi,
                    "week_pnl": week_pnl,
                })
        
        # Sort by all-time PnL descending, return top N
        qualified.sort(key=lambda x: x["alltime_pnl"], reverse=True)
        return qualified[:top_n]
    
    def add_wallet(self, address: str, alias: str = ""):
        """Add a wallet to monitor."""
        addr = address.lower()
        if addr not in self.wallets:
            self.wallets.append(addr)
            if alias:
                self.aliases[addr] = alias
            logger.info(f"➕ Added wallet: {alias or addr}")
    
    def remove_wallet(self, address: str):
        """Remove a wallet from monitoring."""
        addr = address.lower()
        self.wallets = [w for w in self.wallets if w.lower() != addr]
        self._snapshots.pop(addr, None)
        logger.info(f"➖ Removed wallet: {addr[:10]}...")
    
    # ─── Polling ─────────────────────────────────────────────────────────
    
    def _fetch_wallet_state(self, address: str) -> Optional[WalletSnapshot]:
        """Fetch current positions for a single wallet."""
        data = _hl_post({"type": "clearinghouseState", "user": address})
        if not data:
            return None
        
        margin_summary = data.get("marginSummary", {})
        account_value = float(margin_summary.get("accountValue", 0))
        
        positions = {}
        for pos_data in data.get("assetPositions", []):
            pos = pos_data.get("position", {})
            size = float(pos.get("szi", 0))
            
            if abs(size) < 1e-10:
                continue  # Skip zero positions
            
            coin = pos.get("coin", "???")
            entry = float(pos.get("entryPx", 0))
            lev_info = pos.get("leverage", {})
            lev = float(lev_info.get("value", 1)) if isinstance(lev_info, dict) else float(lev_info or 1)
            upnl = float(pos.get("unrealizedPnl", 0))
            margin = float(pos.get("marginUsed", 0))
            liq_px = float(pos.get("liquidationPx", 0) or 0)
            
            positions[coin] = WalletPosition(
                symbol=coin,
                side="LONG" if size > 0 else "SHORT",
                size=abs(size),
                entry_px=entry,
                leverage=lev,
                unrealized_pnl=upnl,
                margin_used=margin,
                liquidation_px=liq_px,
            )
        
        return WalletSnapshot(
            address=address.lower(),
            timestamp=time.time(),
            account_value=account_value,
            positions=positions,
        )
    
    def poll_all(self) -> List[Signal]:
        """
        Poll all wallets and detect changes. Returns new signals.
        
        Change detection:
        - NEW: coin in current but not in previous → OPEN signal
        - CLOSED: coin in previous but not in current → (log only)
        - SIZE_INCREASE: size grew by > 10% → OPEN signal (adding to position)
        - SIZE_DECREASE: size shrunk → (log only, partial close)
        - FLIP: side changed → OPEN signal for new side
        """
        signals = []
        self._poll_count += 1
        
        for address in self.wallets:
            alias = self.aliases.get(address.lower(), address[:8])
            snapshot = self._fetch_wallet_state(address)
            
            if not snapshot:
                logger.debug(f"⚠️ No data for {alias}")
                continue
            
            prev = self._snapshots.get(address.lower())
            
            if prev is None:
                # First poll for this wallet — just save baseline
                self._snapshots[address.lower()] = snapshot
                n = len(snapshot.positions)
                logger.info(f"📸 Baseline for {alias}: {n} positions, ${snapshot.account_value:,.0f}")
                continue
            
            # Compare positions
            prev_coins = set(prev.positions.keys())
            curr_coins = set(snapshot.positions.keys())
            
            # NEW positions
            for coin in curr_coins - prev_coins:
                pos = snapshot.positions[coin]
                sig = self._position_to_signal(pos, address, alias, "NEW")
                if sig:
                    signals.append(sig)
                    logger.info(
                        f"🆕 NEW position: {alias} → {pos.side} {pos.symbol} "
                        f"| Size: {pos.size:.4f} | Entry: ${pos.entry_px:,.2f} "
                        f"| Lev: {pos.leverage:.0f}x"
                    )
            
            # CLOSED positions
            for coin in prev_coins - curr_coins:
                old_pos = prev.positions[coin]
                logger.info(
                    f"🔒 CLOSED position: {alias} → {old_pos.side} {old_pos.symbol} "
                    f"| Was: {old_pos.size:.4f} @ ${old_pos.entry_px:,.2f}"
                )
            
            # CHANGED positions
            for coin in curr_coins & prev_coins:
                curr_pos = snapshot.positions[coin]
                prev_pos = prev.positions[coin]
                
                # Side flip
                if curr_pos.side != prev_pos.side:
                    sig = self._position_to_signal(curr_pos, address, alias, "FLIP")
                    if sig:
                        signals.append(sig)
                    logger.info(
                        f"🔄 FLIP: {alias} → {prev_pos.side}→{curr_pos.side} {coin}"
                    )
                    continue
                
                # Size increase > 10%
                if prev_pos.size > 0:
                    pct_change = (curr_pos.size - prev_pos.size) / prev_pos.size
                    if pct_change > 0.10:
                        sig = self._position_to_signal(curr_pos, address, alias, "ADD")
                        if sig:
                            signals.append(sig)
                        logger.info(
                            f"📈 ADD: {alias} → {curr_pos.side} {coin} "
                            f"+{pct_change*100:.1f}% (new size: {curr_pos.size:.4f})"
                        )
                    elif pct_change < -0.10:
                        logger.info(
                            f"📉 REDUCE: {alias} → {curr_pos.side} {coin} "
                            f"{pct_change*100:.1f}% (new size: {curr_pos.size:.4f})"
                        )
            
            # Update snapshot
            self._snapshots[address.lower()] = snapshot
        
        if signals:
            logger.info(f"📡 Poll #{self._poll_count}: {len(signals)} new signal(s)")
        
        return signals
    
    def _position_to_signal(self, pos: WalletPosition, address: str,
                            alias: str, change_type: str) -> Optional[Signal]:
        """Convert a wallet position change to a Signal."""
        # Map Hyperliquid coin names to KuCoin symbols
        kucoin_symbol = self._map_symbol(pos.symbol)
        if not kucoin_symbol:
            logger.warning(f"Unknown symbol mapping: {pos.symbol}")
            return None
        
        # Default risk parameters
        sl_pct = config.risk.default_sl_pct
        tp_pcts = config.risk.default_tp_pcts.copy()
        leverage = min(int(pos.leverage), config.risk.max_leverage)
        
        signal = Signal(
            symbol=kucoin_symbol,
            side=pos.side,
            entry_price=pos.entry_px,
            sl_pct=sl_pct,
            tp_pcts=tp_pcts,
            leverage=leverage,
            source=f"HL:{alias}:{change_type}",
            wallet_id=address,
            raw_data={
                "coin": pos.symbol,
                "size": pos.size,
                "entry_px": pos.entry_px,
                "leverage": pos.leverage,
                "notional": pos.notional,
                "change_type": change_type,
                "alias": alias,
            },
        )
        
        return signal
    
    @staticmethod
    def _map_symbol(hl_coin: str) -> Optional[str]:
        """Map Hyperliquid coin name to KuCoin Futures symbol."""
        # Common mappings
        mapping = {
            "BTC": "XBTUSDTM",
            "ETH": "ETHUSDTM",
            "SOL": "SOLUSDTM",
            "DOGE": "DOGEUSDTM",
            "XRP": "XRPUSDTM",
            "ADA": "ADAUSDTM",
            "AVAX": "AVAXUSDTM",
            "LINK": "LINKUSDTM",
            "DOT": "DOTUSDTM",
            "MATIC": "MATICUSDTM",
            "ARB": "ARBUSDTM",
            "OP": "OPUSDTM",
            "APT": "APTUSDTM",
            "SUI": "SUIUSDTM",
            "PEPE": "PEPEUSDTM",
            "WIF": "WIFUSDTM",
            "NEAR": "NEARUSDTM",
            "FET": "FETUSDTM",
            "RENDER": "RENDERUSDTM",
            "INJ": "INJUSDTM",
            "TIA": "TIAUSDTM",
            "FIL": "FILUSDTM",
            "ATOM": "ATOMUSDTM",
            "UNI": "UNIUSDTM",
            "LTC": "LTCUSDTM",
            "BCH": "BCHUSDTM",
            "WLD": "WLDUSDTM",
            "RNDR": "RNDRUSDTM",
            "SEI": "SEIUSDTM",
            "AAVE": "AAVEUSDTM",
        }
        
        coin = hl_coin.upper().replace("-", "").replace("/", "")
        
        if coin in mapping:
            return mapping[coin]
        
        # Generic fallback: COINUSDTM
        return f"{coin}USDTM"
    
    # ─── Status ──────────────────────────────────────────────────────────
    
    def get_status(self) -> Dict:
        """Get current monitoring status."""
        active = []
        for addr in self.wallets:
            snap = self._snapshots.get(addr.lower())
            alias = self.aliases.get(addr.lower(), addr[:8])
            if snap:
                active.append({
                    "alias": alias,
                    "positions": len(snap.positions),
                    "account_value": snap.account_value,
                    "last_poll": snap.timestamp,
                    "coins": list(snap.positions.keys()),
                })
            else:
                active.append({"alias": alias, "positions": 0, "status": "awaiting_first_poll"})
        
        return {
            "wallets_tracked": len(self.wallets),
            "poll_count": self._poll_count,
            "details": active,
        }
