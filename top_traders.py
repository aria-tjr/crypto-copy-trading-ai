#!/usr/bin/env python3
"""
Hyperliquid Top Traders Analyzer & Position Monitor
====================================================
Fetches the full leaderboard from Hyperliquid's stats-data endpoint,
ranks traders by various metrics, and monitors their current positions.
"""

import json
import urllib.request
import urllib.error
import time
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────────────────

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# Filters for finding the best traders
MIN_ACCOUNT_VALUE = 10_000       # At least $10K account
MIN_ALL_TIME_PNL = 100_000       # At least $100K all-time profit
MIN_ALL_TIME_ROI = 0.5           # At least 50% all-time ROI
MIN_MONTHLY_VOLUME = 1_000_000   # At least $1M monthly volume (active trader)
TOP_N = 25                       # Show top N traders

# ─── Helpers ─────────────────────────────────────────────────────────────────

def fmt_usd(val):
    """Format a number as USD."""
    if abs(val) >= 1_000_000:
        return f"${val/1_000_000:,.2f}M"
    elif abs(val) >= 1_000:
        return f"${val/1_000:,.1f}K"
    else:
        return f"${val:,.2f}"

def fmt_pct(val):
    """Format a decimal as percentage."""
    return f"{val*100:+.1f}%"

def fmt_addr(addr):
    """Shorten an Ethereum address."""
    return f"{addr[:6]}...{addr[-4:]}"

def hl_post(payload):
    """POST to Hyperliquid info API."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_INFO_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

# ─── Step 1: Fetch & Parse Leaderboard ───────────────────────────────────────

def fetch_leaderboard():
    """Fetch the full leaderboard JSON from Hyperliquid stats."""
    print("=" * 80)
    print("  HYPERLIQUID TOP TRADERS ANALYZER")
    print("=" * 80)
    print(f"\n⏳ Fetching leaderboard from {LEADERBOARD_URL}...")
    
    req = urllib.request.Request(
        LEADERBOARD_URL,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            data = json.loads(raw)
    except Exception as e:
        print(f"❌ Failed to fetch leaderboard: {e}")
        return []
    
    # Parse the leaderboard entries
    if isinstance(data, dict) and "leaderboardRows" in data:
        rows = data["leaderboardRows"]
    elif isinstance(data, list):
        rows = data
    else:
        # Try to find the array in the response
        print(f"   Response type: {type(data)}")
        if isinstance(data, dict):
            print(f"   Keys: {list(data.keys())[:10]}")
            # Try first key that has a list value
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 0:
                    rows = v
                    print(f"   Using key '{k}' with {len(v)} entries")
                    break
            else:
                print("❌ Could not find leaderboard data in response")
                return []
        else:
            return []
    
    print(f"✅ Loaded {len(rows)} traders from leaderboard")
    
    # Parse each trader
    traders = []
    for row in rows:
        try:
            addr = row.get("ethAddress", "")
            acct_val = float(row.get("accountValue", "0"))
            display_name = row.get("displayName") or ""
            
            # Parse window performances
            perfs = {}
            for window_data in row.get("windowPerformances", []):
                if isinstance(window_data, list) and len(window_data) == 2:
                    window_name, metrics = window_data
                    perfs[window_name] = {
                        "pnl": float(metrics.get("pnl", "0")),
                        "roi": float(metrics.get("roi", "0")),
                        "vlm": float(metrics.get("vlm", "0")),
                    }
            
            traders.append({
                "address": addr,
                "accountValue": acct_val,
                "displayName": display_name,
                "day": perfs.get("day", {"pnl": 0, "roi": 0, "vlm": 0}),
                "week": perfs.get("week", {"pnl": 0, "roi": 0, "vlm": 0}),
                "month": perfs.get("month", {"pnl": 0, "roi": 0, "vlm": 0}),
                "allTime": perfs.get("allTime", {"pnl": 0, "roi": 0, "vlm": 0}),
            })
        except Exception as e:
            continue
    
    print(f"✅ Parsed {len(traders)} traders successfully")
    return traders

# ─── Step 2: Filter & Rank ──────────────────────────────────────────────────

def filter_top_traders(traders):
    """Filter and rank traders by profitability and consistency."""
    
    print(f"\n{'─' * 80}")
    print("  FILTERING: Best Profitable & Active Traders")
    print(f"{'─' * 80}")
    print(f"   Min account value: {fmt_usd(MIN_ACCOUNT_VALUE)}")
    print(f"   Min all-time PnL:  {fmt_usd(MIN_ALL_TIME_PNL)}")
    print(f"   Min all-time ROI:  {fmt_pct(MIN_ALL_TIME_ROI)}")
    print(f"   Min monthly volume: {fmt_usd(MIN_MONTHLY_VOLUME)}")
    
    filtered = []
    for t in traders:
        at = t["allTime"]
        mo = t["month"]
        
        # Apply filters
        if t["accountValue"] < MIN_ACCOUNT_VALUE:
            continue
        if at["pnl"] < MIN_ALL_TIME_PNL:
            continue
        if at["roi"] < MIN_ALL_TIME_ROI:
            continue
        if mo["vlm"] < MIN_MONTHLY_VOLUME:
            continue
        
        # Calculate a composite score
        # Weighted: all-time ROI (40%) + month PnL positivity (30%) + consistency (30%)
        month_score = 1.0 if mo["pnl"] > 0 else 0.3
        week_score = 1.0 if t["week"]["pnl"] > 0 else 0.5
        
        t["score"] = (
            at["roi"] * 0.4 +
            month_score * 0.3 +
            week_score * 0.3
        )
        
        filtered.append(t)
    
    print(f"\n   ✅ {len(filtered)} traders passed all filters (from {len(traders)} total)")
    
    # Sort by all-time PnL descending
    filtered.sort(key=lambda x: x["allTime"]["pnl"], reverse=True)
    
    return filtered[:TOP_N]

# ─── Step 3: Display Rankings ────────────────────────────────────────────────

def display_rankings(traders):
    """Display the top traders in a formatted table."""
    
    print(f"\n{'=' * 80}")
    print(f"  🏆 TOP {len(traders)} HYPERLIQUID FUTURES TRADERS")
    print(f"{'=' * 80}")
    
    for i, t in enumerate(traders, 1):
        name = t["displayName"] or fmt_addr(t["address"])
        at = t["allTime"]
        mo = t["month"]
        wk = t["week"]
        dy = t["day"]
        
        # Status indicators
        month_icon = "🟢" if mo["pnl"] > 0 else "🔴"
        week_icon = "🟢" if wk["pnl"] > 0 else "🔴"
        day_icon = "🟢" if dy["pnl"] > 0 else "🔴"
        
        print(f"\n{'─' * 80}")
        print(f"  #{i:>2}  {name}")
        print(f"       Address: {t['address']}")
        print(f"       Account Value: {fmt_usd(t['accountValue'])}")
        print(f"       ┌─────────────┬──────────────────┬──────────────┬──────────────────┐")
        print(f"       │   Period    │       PnL        │     ROI      │     Volume       │")
        print(f"       ├─────────────┼──────────────────┼──────────────┼──────────────────┤")
        print(f"       │ {day_icon} Today    │ {fmt_usd(dy['pnl']):>16s} │ {fmt_pct(dy['roi']):>12s} │ {fmt_usd(dy['vlm']):>16s} │")
        print(f"       │ {week_icon} Week     │ {fmt_usd(wk['pnl']):>16s} │ {fmt_pct(wk['roi']):>12s} │ {fmt_usd(wk['vlm']):>16s} │")
        print(f"       │ {month_icon} Month    │ {fmt_usd(mo['pnl']):>16s} │ {fmt_pct(mo['roi']):>12s} │ {fmt_usd(mo['vlm']):>16s} │")
        print(f"       │ 📊 All-Time │ {fmt_usd(at['pnl']):>16s} │ {fmt_pct(at['roi']):>12s} │ {fmt_usd(at['vlm']):>16s} │")
        print(f"       └─────────────┴──────────────────┴──────────────┴──────────────────┘")

# ─── Step 4: Get Current Positions ───────────────────────────────────────────

def get_positions(address):
    """Fetch current open positions for a trader."""
    result = hl_post({"type": "clearinghouseState", "user": address})
    if "error" in result:
        return []
    
    positions = []
    for pos in result.get("assetPositions", []):
        p = pos.get("position", {})
        size = float(p.get("szi", "0"))
        if size == 0:
            continue
        
        entry = float(p.get("entryPx", "0"))
        mark = float(p.get("positionValue", "0"))
        unrealized = float(p.get("unrealizedPnl", "0"))
        leverage_type = p.get("leverage", {}).get("type", "")
        leverage_val = float(p.get("leverage", {}).get("value", "0"))
        coin = p.get("coin", "???")
        side = "LONG" if size > 0 else "SHORT"
        
        positions.append({
            "coin": coin,
            "side": side,
            "size": abs(size),
            "entryPx": entry,
            "unrealizedPnl": unrealized,
            "leverage": f"{leverage_val:.0f}x {leverage_type}",
            "positionValue": abs(mark),
        })
    
    return positions

def monitor_top_positions(traders, n=10):
    """Show current positions for the top N traders."""
    
    print(f"\n{'=' * 80}")
    print(f"  📡 LIVE POSITIONS OF TOP {min(n, len(traders))} TRADERS")
    print(f"  (querying Hyperliquid API...)")
    print(f"{'=' * 80}")
    
    for i, t in enumerate(traders[:n], 1):
        name = t["displayName"] or fmt_addr(t["address"])
        print(f"\n{'─' * 80}")
        print(f"  #{i} {name} ({t['address']})")
        print(f"     Account: {fmt_usd(t['accountValue'])} | All-time PnL: {fmt_usd(t['allTime']['pnl'])}")
        
        positions = get_positions(t["address"])
        
        if not positions:
            print(f"     💤 No open positions")
            continue
        
        print(f"     📊 {len(positions)} open position(s):")
        print(f"     {'Coin':<8} {'Side':<6} {'Size':>12} {'Entry':>12} {'Unrealized PnL':>16} {'Leverage':>12}")
        print(f"     {'─'*8} {'─'*6} {'─'*12} {'─'*12} {'─'*16} {'─'*12}")
        
        for p in positions:
            pnl_icon = "🟢" if p["unrealizedPnl"] >= 0 else "🔴"
            print(f"     {p['coin']:<8} {p['side']:<6} {p['size']:>12.4f} {p['entryPx']:>12.2f} {pnl_icon}{fmt_usd(p['unrealizedPnl']):>14s} {p['leverage']:>12}")
        
        time.sleep(0.2)  # Rate limiting

# ─── Step 5: Summary & Copy Trading Recommendations ─────────────────────────

def show_recommendations(traders):
    """Show actionable copy trading recommendations."""
    
    print(f"\n{'=' * 80}")
    print(f"  📋 COPY TRADING RECOMMENDATIONS")
    print(f"{'=' * 80}")
    
    # Find traders who are currently profitable this month AND have open positions
    active_profitable = []
    for t in traders[:15]:
        if t["month"]["pnl"] > 0 and t["week"]["pnl"] > 0:
            positions = get_positions(t["address"])
            if positions:
                active_profitable.append((t, positions))
        time.sleep(0.15)
    
    if active_profitable:
        print(f"\n  🌟 BEST TO COPY NOW (profitable this month + week, with open positions):")
        for t, positions in active_profitable:
            name = t["displayName"] or fmt_addr(t["address"])
            print(f"\n  ► {name}")
            print(f"    Address: {t['address']}")
            print(f"    Month PnL: {fmt_usd(t['month']['pnl'])} | Week PnL: {fmt_usd(t['week']['pnl'])}")
            print(f"    All-time: {fmt_usd(t['allTime']['pnl'])} ({fmt_pct(t['allTime']['roi'])} ROI)")
            print(f"    Current trades:")
            for p in positions:
                pnl_icon = "🟢" if p["unrealizedPnl"] >= 0 else "🔴"
                print(f"      • {p['side']} {p['coin']} @ {p['entryPx']:.2f} | {pnl_icon} {fmt_usd(p['unrealizedPnl'])}")
    else:
        print("\n  ⚠️  No traders currently meet all criteria (profitable month+week with open positions)")
        print("  💡 Top traders by all-time PnL are still listed above for monitoring")
    
    # Save addresses for monitoring
    print(f"\n{'─' * 80}")
    print(f"  📝 ADDRESSES TO MONITOR (paste into your copy-bot):")
    print(f"{'─' * 80}")
    for i, t in enumerate(traders[:10], 1):
        name = t["displayName"] or "anon"
        print(f"  {i:>2}. {t['address']}  # {name} | PnL: {fmt_usd(t['allTime']['pnl'])}")

# ─── Step 6: Quick Stats ────────────────────────────────────────────────────

def show_leaderboard_stats(traders):
    """Show overall leaderboard statistics."""
    
    total = len(traders)
    profitable = sum(1 for t in traders if t["allTime"]["pnl"] > 0)
    total_pnl = sum(t["allTime"]["pnl"] for t in traders)
    avg_acct = sum(t["accountValue"] for t in traders) / total if total else 0
    
    print(f"\n{'─' * 80}")
    print(f"  📊 LEADERBOARD STATS")
    print(f"{'─' * 80}")
    print(f"  Total traders on leaderboard: {total}")
    print(f"  Profitable (all-time):        {profitable} ({profitable/total*100:.0f}%)")
    print(f"  Total PnL on leaderboard:     {fmt_usd(total_pnl)}")
    print(f"  Average account value:         {fmt_usd(avg_acct)}")
    
    # Top by different metrics
    by_pnl = sorted(traders, key=lambda x: x["allTime"]["pnl"], reverse=True)[:5]
    by_roi = sorted(traders, key=lambda x: x["allTime"]["roi"], reverse=True)[:5]
    by_acct = sorted(traders, key=lambda x: x["accountValue"], reverse=True)[:5]
    
    print(f"\n  💰 Highest All-Time PnL:")
    for t in by_pnl:
        name = t["displayName"] or fmt_addr(t["address"])
        print(f"     {fmt_usd(t['allTime']['pnl']):>14s}  {name}")
    
    print(f"\n  📈 Highest All-Time ROI:")
    for t in by_roi:
        name = t["displayName"] or fmt_addr(t["address"])
        print(f"     {fmt_pct(t['allTime']['roi']):>14s}  {name} (acct: {fmt_usd(t['accountValue'])})")
    
    print(f"\n  🏦 Largest Accounts:")
    for t in by_acct:
        name = t["displayName"] or fmt_addr(t["address"])
        print(f"     {fmt_usd(t['accountValue']):>14s}  {name}")

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    start = time.time()
    
    # 1. Fetch leaderboard
    all_traders = fetch_leaderboard()
    if not all_traders:
        return
    
    # 2. Show overall stats
    show_leaderboard_stats(all_traders)
    
    # 3. Filter & rank
    top = filter_top_traders(all_traders)
    if not top:
        print("\n❌ No traders matched all filters. Try relaxing the criteria.")
        return
    
    # 4. Display rankings
    display_rankings(top)
    
    # 5. Monitor positions
    monitor_top_positions(top, n=10)
    
    # 6. Recommendations
    show_recommendations(top)
    
    elapsed = time.time() - start
    print(f"\n{'=' * 80}")
    print(f"  ⏱️  Completed in {elapsed:.1f}s")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 80}")

if __name__ == "__main__":
    main()
