"""
KuCoin Futures Execution Engine — Order management and position tracking.
=========================================================================
Handles order placement, SL/TP, trailing stops, and position monitoring.
Supports both paper trading and live execution.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Tuple

from .config import config
from .signals import Signal, SignalDB

logger = logging.getLogger(__name__)


class KuCoinExecutor:
    """
    KuCoin Futures API executor.
    
    Features:
    - Authenticated API calls (HMAC-SHA256)
    - Market/limit orders with SL/TP
    - Trailing stop loss management
    - Position monitoring and management
    - Paper trading mode (logs trades without executing)
    """
    
    def __init__(self, db: SignalDB, paper: Optional[bool] = None):
        self.db = db
        self.cfg = config.kucoin
        self.paper = paper if paper is not None else config.pipeline.paper_trading
        
        # Use master account keys for futures if sub-account doesn't have permissions
        self._api_key = self.cfg.master_api_key
        self._api_secret = self.cfg.master_api_secret
        self._api_passphrase = self.cfg.master_api_passphrase
        
        if self.paper:
            logger.info("📝 KuCoin Executor: PAPER TRADING MODE")
        else:
            logger.info("🔴 KuCoin Executor: LIVE TRADING MODE")
    
    # ─── Authentication ──────────────────────────────────────────────────
    
    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> str:
        """Generate KC-API-SIGN header."""
        msg = timestamp + method.upper() + endpoint + body
        return base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                msg.encode(),
                hashlib.sha256
            ).digest()
        ).decode()
    
    def _sign_passphrase(self) -> str:
        """Generate KC-API-PASSPHRASE (HMAC-encrypted for v2)."""
        return base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                self._api_passphrase.encode(),
                hashlib.sha256
            ).digest()
        ).decode()
    
    def _request(self, method: str, endpoint: str, body: str = "", 
                 base_url: Optional[str] = None) -> Dict:
        """Make authenticated API request."""
        base = base_url or self.cfg.futures_base_url
        timestamp = str(int(time.time() * 1000))
        
        headers = {
            "KC-API-KEY": self._api_key,
            "KC-API-SIGN": self._sign(timestamp, method, endpoint, body),
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": self._sign_passphrase(),
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }
        
        url = base + endpoint
        data = body.encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            try:
                return json.loads(error_body)
            except json.JSONDecodeError:
                return {"code": str(e.code), "msg": error_body}
        except Exception as e:
            return {"code": "ERROR", "msg": str(e)}
    
    def _public_request(self, endpoint: str) -> Dict:
        """Make unauthenticated public API request."""
        url = self.cfg.futures_base_url + endpoint
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"code": "ERROR", "msg": str(e)}
    
    # ─── Account Info ────────────────────────────────────────────────────
    
    def get_account_overview(self) -> Optional[Dict]:
        """Get futures account balance overview."""
        result = self._request("GET", "/api/v1/account-overview")
        if result.get("code") == "200000":
            data = result.get("data", {})
            logger.info(
                f"💰 Account: balance=${float(data.get('accountEquity', 0)):,.2f} "
                f"| available=${float(data.get('availableBalance', 0)):,.2f} "
                f"| unrealized PnL=${float(data.get('unrealisedPNL', 0)):,.2f}"
            )
            return data
        logger.warning(f"Failed to get account overview: {result}")
        return None
    
    def get_current_capital(self) -> float:
        """Get current account equity."""
        overview = self.get_account_overview()
        if overview:
            return float(overview.get("accountEquity", 0))
        return config.risk.initial_capital  # Fallback
    
    # ─── Market Data ─────────────────────────────────────────────────────
    
    def get_ticker(self, symbol: str = "XBTUSDTM") -> Optional[Dict]:
        """Get current ticker for a symbol."""
        result = self._public_request(f"/api/v1/ticker?symbol={symbol}")
        if result.get("code") == "200000":
            return result.get("data")
        return None
    
    def get_mark_price(self, symbol: str = "XBTUSDTM") -> Optional[float]:
        """Get current mark price."""
        ticker = self.get_ticker(symbol)
        if ticker:
            return float(ticker.get("price", 0))
        return None
    
    def get_contract_detail(self, symbol: str = "XBTUSDTM") -> Optional[Dict]:
        """Get contract specification (lot size, tick size, etc)."""
        result = self._public_request(f"/api/v1/contracts/{symbol}")
        if result.get("code") == "200000":
            return result.get("data")
        return None
    
    # ─── Order Execution ─────────────────────────────────────────────────
    
    def open_position(self, signal: Signal) -> Tuple[bool, str]:
        """
        Execute a trade based on signal.
        
        1. Places market order
        2. Sets stop loss
        3. Sets take profit (partial exits)
        
        Returns: (success, order_id_or_error)
        """
        symbol = signal.symbol or "XBTUSDTM"
        side = "buy" if signal.side == "LONG" else "sell"
        
        # Get current price for market order reference
        current_price = self.get_mark_price(symbol)
        if not current_price:
            return False, "Failed to get current price"
        
        # Update entry price to current if not set
        if signal.entry_price <= 0:
            signal.entry_price = current_price
        
        # Calculate lot size
        contract = self.get_contract_detail(symbol)
        if not contract:
            return False, "Failed to get contract details"
        
        multiplier = float(contract.get("multiplier", 1))
        lot_size = int(contract.get("lotSize", 1))
        
        # Convert coin size to contracts
        size_in_contracts = int(signal.final_size / multiplier) if multiplier > 0 else 0
        size_in_contracts = max(size_in_contracts, lot_size)  # Minimum 1 lot
        
        if self.paper:
            return self._paper_execute(signal, symbol, side, size_in_contracts, current_price)
        
        return self._live_execute(signal, symbol, side, size_in_contracts, current_price)
    
    def _paper_execute(self, signal: Signal, symbol: str, side: str,
                       size: int, price: float) -> Tuple[bool, str]:
        """Paper trade execution — log without placing real orders."""
        order_id = f"PAPER-{signal.id}-{int(time.time())}"
        
        logger.info(
            f"📝 PAPER TRADE: {side.upper()} {size} lots {symbol} "
            f"@ ${price:,.2f} | {signal.leverage}x | "
            f"SL: {signal.sl_pct*100:+.1f}% | TP: {[f'{t*100:+.1f}%' for t in signal.tp_pcts]}"
        )
        
        signal.status = "EXECUTED"
        signal.order_id = order_id
        signal.fill_price = price
        signal.entry_price = price
        self.db.save_signal(signal)
        
        return True, order_id
    
    def _live_execute(self, signal: Signal, symbol: str, side: str,
                      size: int, price: float) -> Tuple[bool, str]:
        """Live order execution."""
        import uuid
        
        # 1. Place market order
        order_body = {
            "clientOid": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "type": "market",
            "size": size,
            "leverage": str(signal.leverage),
        }
        
        logger.info(f"🔴 LIVE ORDER: {json.dumps(order_body)}")
        
        result = self._request("POST", "/api/v1/orders", json.dumps(order_body))
        
        if result.get("code") != "200000":
            error = result.get("msg", result)
            logger.error(f"❌ Order failed: {error}")
            signal.status = "BLOCKED"
            signal.close_reason = f"order_failed: {error}"
            self.db.save_signal(signal)
            return False, str(error)
        
        order_id = result.get("data", {}).get("orderId", "")
        logger.info(f"✅ Order placed: {order_id}")
        
        signal.status = "EXECUTED"
        signal.order_id = order_id
        signal.fill_price = price
        signal.entry_price = price
        self.db.save_signal(signal)
        
        # 2. Place stop loss
        self._place_stop_loss(signal, symbol, size)
        
        # 3. Place take profit (first target)
        self._place_take_profit(signal, symbol, size)
        
        return True, order_id
    
    def _place_stop_loss(self, signal: Signal, symbol: str, size: int):
        """Place stop loss order."""
        sl_side = "sell" if signal.side == "LONG" else "buy"
        sl_price = signal.sl_price
        
        if self.paper:
            logger.info(f"📝 PAPER SL: {sl_side} {size} lots @ ${sl_price:,.2f}")
            return
        
        import uuid
        sl_body = {
            "clientOid": str(uuid.uuid4()),
            "symbol": symbol,
            "side": sl_side,
            "type": "market",
            "size": size,
            "stop": "down" if signal.side == "LONG" else "up",
            "stopPrice": str(round(sl_price, 2)),
            "stopPriceType": "TP",  # Trade price
        }
        
        result = self._request("POST", "/api/v1/orders", json.dumps(sl_body))
        if result.get("code") == "200000":
            logger.info(f"✅ SL placed @ ${sl_price:,.2f}")
        else:
            logger.warning(f"⚠️ SL placement failed: {result}")
    
    def _place_take_profit(self, signal: Signal, symbol: str, total_size: int):
        """Place take profit orders (partial exits)."""
        tp_side = "sell" if signal.side == "LONG" else "buy"
        partial_pcts = config.risk.tp_partial_pcts
        
        for i, (tp_pct, partial) in enumerate(zip(signal.tp_pcts, partial_pcts)):
            tp_price = signal.tp_prices[i] if i < len(signal.tp_prices) else 0
            tp_size = max(1, int(total_size * partial))
            
            if self.paper:
                logger.info(f"📝 PAPER TP{i+1}: {tp_side} {tp_size} lots @ ${tp_price:,.2f}")
                continue
            
            import uuid
            tp_body = {
                "clientOid": str(uuid.uuid4()),
                "symbol": symbol,
                "side": tp_side,
                "type": "market",
                "size": tp_size,
                "stop": "up" if signal.side == "LONG" else "down",
                "stopPrice": str(round(tp_price, 2)),
                "stopPriceType": "TP",
            }
            
            result = self._request("POST", "/api/v1/orders", json.dumps(tp_body))
            if result.get("code") == "200000":
                logger.info(f"✅ TP{i+1} placed @ ${tp_price:,.2f} ({partial*100:.0f}% of position)")
            else:
                logger.warning(f"⚠️ TP{i+1} placement failed: {result}")
    
    # ─── Position Management ─────────────────────────────────────────────
    
    def get_open_positions(self) -> List[Dict]:
        """Get all open positions from exchange."""
        if self.paper:
            return self._get_paper_positions()
        
        result = self._request("GET", "/api/v1/positions")
        if result.get("code") == "200000":
            positions = result.get("data", [])
            return [p for p in positions if float(p.get("currentQty", 0)) != 0]
        return []
    
    def _get_paper_positions(self) -> List[Dict]:
        """Get paper positions from DB."""
        signals = self.db.get_open_positions()
        positions = []
        for s in signals:
            mark = self.get_mark_price(s.symbol or "XBTUSDTM")
            if mark:
                pnl = (mark - s.fill_price) * s.final_size
                if s.side == "SHORT":
                    pnl = -pnl
                positions.append({
                    "symbol": s.symbol,
                    "side": s.side,
                    "size": s.final_size,
                    "entry": s.fill_price,
                    "mark": mark,
                    "pnl": pnl,
                    "signal_id": s.id,
                })
        return positions
    
    def check_trailing_stop(self):
        """
        Check and update trailing stops for all open positions.
        
        Logic:
        1. If price moves +2% from entry → move SL to breakeven
        2. Trail SL with 0.5% offset
        """
        if not self.paper:
            return  # Trailing logic handled by exchange for live trades
        
        signals = self.db.get_open_positions()
        for signal in signals:
            mark = self.get_mark_price(signal.symbol or "XBTUSDTM")
            if not mark or not signal.fill_price:
                continue
            
            # Calculate current P&L percentage
            if signal.side == "LONG":
                pnl_pct = (mark - signal.fill_price) / signal.fill_price
            else:
                pnl_pct = (signal.fill_price - mark) / signal.fill_price
            
            # Check SL hit (paper mode)
            sl_pct = abs(signal.sl_pct)
            if pnl_pct <= -sl_pct:
                loss = signal.fill_price * sl_pct * signal.final_size
                if signal.side == "SHORT":
                    loss = -loss
                logger.info(f"🔴 PAPER SL HIT: {signal.summary()} | Loss: ${loss:,.2f}")
                signal.status = "CLOSED"
                signal.realized_pnl = -abs(loss)
                signal.closed_at = time.time()
                signal.close_reason = "SL"
                self.db.save_signal(signal)
                continue
            
            # Check TP hits
            for i, tp_pct in enumerate(signal.tp_pcts):
                if pnl_pct >= tp_pct:
                    profit = signal.fill_price * tp_pct * signal.final_size
                    partial = config.risk.tp_partial_pcts[i] if i < len(config.risk.tp_partial_pcts) else 0.5
                    profit *= partial
                    logger.info(f"🟢 PAPER TP{i+1} HIT: {signal.summary()} | Profit: ${profit:,.2f}")
                    
                    if i == len(signal.tp_pcts) - 1:
                        # Full close on last TP
                        signal.status = "CLOSED"
                        signal.realized_pnl = profit
                        signal.closed_at = time.time()
                        signal.close_reason = f"TP{i+1}"
                        self.db.save_signal(signal)
                    break
            
            # Trail SL to breakeven after +2%
            activation = config.risk.trail_sl_activation_pct
            if pnl_pct >= activation:
                # Move SL to breakeven (0%) or trailing offset
                new_sl = -config.risk.trail_sl_offset_pct
                if signal.sl_pct < new_sl:  # SL is currently worse than BE
                    signal.sl_pct = new_sl
                    self.db.save_signal(signal)
                    logger.info(f"📈 Trailing SL updated: {signal.id} → SL at {new_sl*100:+.1f}%")
    
    # ─── Close Position ──────────────────────────────────────────────────
    
    def close_position(self, signal: Signal, reason: str = "MANUAL") -> bool:
        """Close an open position."""
        if self.paper:
            mark = self.get_mark_price(signal.symbol or "XBTUSDTM")
            if mark and signal.fill_price:
                if signal.side == "LONG":
                    pnl = (mark - signal.fill_price) * signal.final_size
                else:
                    pnl = (signal.fill_price - mark) * signal.final_size
                signal.realized_pnl = pnl
            
            signal.status = "CLOSED"
            signal.closed_at = time.time()
            signal.close_reason = reason
            self.db.save_signal(signal)
            logger.info(f"📝 PAPER CLOSE: {signal.summary()} | PnL: ${signal.realized_pnl:,.2f}")
            return True
        
        # Live close: place opposing market order
        import uuid
        close_side = "sell" if signal.side == "LONG" else "buy"
        
        body = {
            "clientOid": str(uuid.uuid4()),
            "symbol": signal.symbol,
            "side": close_side,
            "type": "market",
            "size": int(signal.final_size),
            "closeOrder": True,
        }
        
        result = self._request("POST", "/api/v1/orders", json.dumps(body))
        if result.get("code") == "200000":
            signal.status = "CLOSED"
            signal.closed_at = time.time()
            signal.close_reason = reason
            self.db.save_signal(signal)
            logger.info(f"✅ Position closed: {signal.id} ({reason})")
            return True
        
        logger.error(f"❌ Failed to close position: {result}")
        return False
