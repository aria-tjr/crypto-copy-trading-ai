"""
Find the best futures traders from third-party platforms.
Sources:
  1. Hyperliquid - On-chain perp DEX with public vault data & user lookups
  2. Copin.io   - Aggregates traders across 50+ perp DEXes

No API keys needed - all public data.
"""

import json
import urllib.request
import urllib.error
from datetime import datetime


# ============================================================
#  HYPERLIQUID - Public API (no key needed)
# ============================================================

HL_API = "https://api.hyperliquid.xyz/info"


def hl_post(payload):
    """POST to Hyperliquid info API."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        HL_API,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")[:300]
        print(f"  [HL HTTP {e.code}] {body}")
        return None
    except Exception as e:
        print(f"  [HL ERROR] {e}")
        return None


def try_hl_leaderboard():
    """Try multiple payload formats to get the Hyperliquid leaderboard."""
    print("\n" + "=" * 70)
    print("  HYPERLIQUID - Leaderboard (trying multiple formats)")
    print("=" * 70)

    # The leaderboard is undocumented. Try known payload variations.
    payloads = [
        {"type": "leaderboard"},
        {"type": "leaderboard", "timeWindow": "allTime"},
        {"type": "leaderboard", "timeWindow": "1d"},
        {"type": "leaderboard", "timeWindow": "7d"},
        {"type": "leaderboard", "timeWindow": "30d"},
        {"type": "leaderboard", "window": "allTime"},
        {"type": "leaderboard", "period": "all"},
        {"type": "topTraders"},
        {"type": "topTraders", "window": "allTime"},
        {"type": "rankings"},
    ]

    for p in payloads:
        result = hl_post(p)
        if result and isinstance(result, list) and len(result) > 0:
            print(f"  ✅ Leaderboard found with payload: {p}")
            print(f"  Got {len(result)} entries\n")
            if isinstance(result[0], dict):
                print(f"  Sample entry keys: {list(result[0].keys())}")
            return result
        elif result and isinstance(result, dict) and result.get("data"):
            print(f"  ✅ Leaderboard found with payload: {p}")
            return result["data"]

    print("  ❌ Leaderboard endpoint is not publicly accessible via API.")
    print("     (It's a frontend-only feature at app.hyperliquid.xyz/leaderboard)")
    print("     Using alternative methods to find top traders...\n")
    return None


def get_hl_top_vaults():
    """Fetch top Hyperliquid vaults (managed trading funds)."""
    print("\n" + "=" * 70)
    print("  HYPERLIQUID - Top Vaults (Managed Funds)")
    print("=" * 70)
    print("  Vaults are managed trading funds run by top traders.")
    print("  You can deposit directly into vaults to copy their strategy.\n")

    # Known top vault addresses (discoverable at app.hyperliquid.xyz/vaults)
    known_vaults = [
        ("HLP (Hyperliquidity Provider)", "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"),
        ("Hyperliquid Assistance Fund", "0x2eee99b89e0d7e4be79aa2e73fbcab4f4dd2b3a9"),
    ]

    vault_results = []

    for name, addr in known_vaults:
        result = hl_post({"type": "vaultDetails", "vaultAddress": addr})
        if result:
            vault_name = result.get("name", name)
            leader = result.get("leader", "?")
            apr = result.get("apr", 0)
            desc = result.get("description", "")[:120]
            followers = result.get("followers", [])
            is_closed = result.get("isClosed", False)

            portfolio = result.get("portfolio", [])
            all_time_pnl = "N/A"
            for period_data in portfolio:
                if isinstance(period_data, list) and len(period_data) >= 2:
                    if period_data[0] == "allTime":
                        pnl_hist = period_data[1].get("pnlHistory", [])
                        if pnl_hist:
                            all_time_pnl = pnl_hist[-1][1] if pnl_hist[-1] else "N/A"

            print(f"  📊 Vault: {vault_name}")
            if len(leader) > 16:
                print(f"     Leader: {leader[:10]}...{leader[-6:]}")
            else:
                print(f"     Leader: {leader}")
            if isinstance(apr, (int, float)):
                print(f"     APR: {apr*100:.2f}%")
            else:
                print(f"     APR: {apr}")
            print(f"     Followers: {len(followers)}")
            print(f"     Status: {'Closed' if is_closed else 'Open'}")
            if desc:
                print(f"     Desc: {desc}")
            try:
                pnl_val = float(all_time_pnl)
                print(f"     All-time PnL: ${pnl_val:,.0f}")
            except (ValueError, TypeError):
                print(f"     All-time PnL: {all_time_pnl}")
            print()

            vault_results.append({
                "name": vault_name,
                "address": addr,
                "leader": leader,
                "apr": apr,
                "followers": len(followers),
                "allTimePnl": all_time_pnl,
            })

    return vault_results


def get_hl_user_state(address, label=""):
    """Get a specific trader's positions and account state from Hyperliquid."""
    result = hl_post({"type": "clearinghouseState", "user": address})
    if not result:
        return None

    margin = result.get("marginSummary", result.get("crossMarginSummary", {}))
    positions = result.get("assetPositions", [])
    acct_val = margin.get("accountValue", "0")
    total_pos = margin.get("totalNtlPos", "0")

    tag = label or f"{address[:8]}...{address[-6:]}"
    try:
        av = float(acct_val)
        tp = float(total_pos)
    except ValueError:
        av = tp = 0

    print(f"\n  👤 {tag}")
    print(f"     Address: {address}")
    print(f"     Account Value: ${av:,.2f}")
    print(f"     Total Notional: ${tp:,.2f}")

    if positions:
        print(f"     Open Positions ({len(positions)}):")
        for pos in positions:
            p = pos.get("position", pos)
            coin = p.get("coin", "?")
            sz = p.get("szi", "0")
            entry = p.get("entryPx", "?")
            unreal = p.get("unrealizedPnl", "0")
            lev = p.get("leverage", {})
            lev_val = lev.get("value", "?") if isinstance(lev, dict) else lev
            try:
                sz_f = float(sz)
                side = "LONG" if sz_f > 0 else "SHORT"
            except ValueError:
                side = "?"
            try:
                upnl = float(unreal)
                upnl_str = f"${upnl:>+,.2f}"
            except ValueError:
                upnl_str = str(unreal)
            print(f"       {coin:<10} {side:<6} Size:{sz:<14} Entry:{entry:<14} uPnL:{upnl_str}  Lev:{lev_val}x")
    else:
        print("     No open positions")
    return result


def get_hl_portfolio(address, label=""):
    """Get portfolio performance for a Hyperliquid user."""
    result = hl_post({"type": "portfolio", "user": address})
    if not result:
        return None

    tag = label or f"{address[:8]}...{address[-6:]}"
    print(f"\n  📈 Portfolio: {tag}")

    for period in result:
        if isinstance(period, list) and len(period) >= 2:
            period_name = period[0]
            data = period[1]
            if isinstance(data, dict):
                vlm = data.get("vlm", "0")
                pnl_hist = data.get("pnlHistory", [])
                acct_hist = data.get("accountValueHistory", [])

                final_pnl = pnl_hist[-1][1] if pnl_hist else "N/A"
                final_av = acct_hist[-1][1] if acct_hist else "N/A"

                try:
                    pnl_str = f"${float(final_pnl):>+,.0f}"
                except (ValueError, TypeError):
                    pnl_str = str(final_pnl)

                try:
                    av_str = f"${float(final_av):>,.0f}"
                except (ValueError, TypeError):
                    av_str = str(final_av)

                try:
                    vlm_str = f"${float(vlm):>,.0f}"
                except (ValueError, TypeError):
                    vlm_str = str(vlm)

                print(f"     {period_name:<12}  PnL: {pnl_str:<16}  AcctVal: {av_str:<16}  Volume: {vlm_str}")

    return result


# ============================================================
#  KNOWN WHALE / TOP TRADER ADDRESSES (Hyperliquid)
# ============================================================

# These are publicly known top Hyperliquid traders from leaderboards,
# Twitter/X, and on-chain analysis. Addresses are public on-chain data.
KNOWN_HL_WHALES = [
    ("James Wynn (large BTC/ETH)", "0x2ffe23eb3c8e90d1de7bfe03bad6e0c8a6f76c63"),
    ("qwatio (Crypto Twitter)", "0xe8576f8ae4e150e77bb68f52e1439a0c6dfe3b17"),
    ("Whale - Top 5 Leaderboard", "0x344bfc3bf14b05e45daeef1877c8b1b08d3c4b52"),
    ("SDK Test User", "0x5e9ee1089755c3435139848e47e6635505d5a13a"),
    ("Large Trader A", "0x20f9628a485ebbb1e6ccb98f783e35d4eedc0e25"),
]


def inspect_known_whales():
    """Look up known top trader addresses on Hyperliquid."""
    print("\n" + "=" * 70)
    print("  HYPERLIQUID - Known Top Trader Positions (Live)")
    print("=" * 70)
    print("  Checking real-time positions of well-known Hyperliquid traders...\n")

    active = []
    for name, addr in KNOWN_HL_WHALES:
        state = get_hl_user_state(addr, label=name)
        if state:
            positions = state.get("assetPositions", [])
            margin = state.get("marginSummary", {})
            try:
                av = float(margin.get("accountValue", "0"))
            except ValueError:
                av = 0
            if av > 0 or positions:
                active.append({"name": name, "address": addr, "accountValue": av, "positions": len(positions)})

    if active:
        print(f"\n  --- Summary: {len(active)} active traders ---")
        active.sort(key=lambda x: x["accountValue"], reverse=True)
        for t in active:
            print(f"  {t['name']:<40} AcctVal: ${t['accountValue']:>12,.0f}  Positions: {t['positions']}")

    return active


# ============================================================
#  COPIN.IO
# ============================================================

COPIN_PUBLIC = "https://api.copin.io"


def get_copin_top_traders():
    """Try to access Copin.io trader data via their public API."""
    print("\n" + "=" * 70)
    print("  COPIN.IO - Top Perpetual DEX Traders (multi-chain)")
    print("=" * 70)
    print("  Copin tracks traders across GMX, dYdX, Kwenta, Hyperliquid, etc.\n")

    endpoints = [
        ("/api/top-traders", {"protocol": "HYPERLIQUID", "limit": "20"}),
        ("/api/public/traders/top", {"protocol": "HYPERLIQUID", "limit": "20"}),
        ("/public/top-traders", {"protocol": "HYPERLIQUID", "limit": "20"}),
        ("/v1/public/traders", {"limit": "20", "sortBy": "pnl30D"}),
    ]

    for ep, params in endpoints:
        url = COPIN_PUBLIC + ep
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url += "?" + qs

        req = urllib.request.Request(
            url,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data:
                    items = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(items, list) and len(items) > 0:
                        print(f"  ✅ Found {len(items)} traders via {ep}")
                        for i, t in enumerate(items[:15], 1):
                            addr = t.get("account", t.get("address", t.get("id", "?")))
                            pnl = t.get("pnl", t.get("pnl30D", t.get("realisedPnl", "?")))
                            wr = t.get("winRate", t.get("winrate", "?"))
                            addr_short = f"{addr[:8]}...{addr[-4:]}" if len(str(addr)) > 14 else addr
                            try:
                                pnl_str = f"${float(pnl):>+,.0f}"
                            except:
                                pnl_str = str(pnl)[:15]
                            print(f"    {i:<4} {addr_short:<20} PnL: {pnl_str}")
                        return items
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")[:150]
            print(f"  [{e.code}] {ep}: {body}")
        except Exception as e:
            print(f"  [ERR] {ep}: {e}")

    print()
    print("  ℹ️  Copin.io API requires authentication for trader explorer queries.")
    print("  Browse top traders manually at:")
    print("    👉 https://app.copin.io/explorer")
    print("    👉 https://app.copin.io/leaderboard")
    print()
    print("  Copin offers COPY TRADING to CEXes (BingX, Bitget, Gate):")
    print("    - Monitor on-chain DEX traders → auto-mirror to your CEX account")
    print("    - Free tier available → https://docs.copin.io/features/centralized-copy-trading-ccp")
    return []


# ============================================================
#  NETWORK STATS
# ============================================================

def get_hl_stats():
    """Get Hyperliquid network stats and top markets."""
    print("\n" + "=" * 70)
    print("  HYPERLIQUID - Network Stats & Top Markets")
    print("=" * 70)

    result = hl_post({"type": "perpMarketStatus"})
    if result:
        net_deposit = result.get("totalNetDeposit", "?")
        try:
            print(f"  Total Net Deposits: ${float(net_deposit):,.0f}")
        except:
            print(f"  Total Net Deposits: {net_deposit}")

    result = hl_post({"type": "metaAndAssetCtxs"})
    if result and isinstance(result, list) and len(result) >= 2:
        universe = result[0].get("universe", []) if isinstance(result[0], dict) else []
        contexts = result[1] if len(result) > 1 else []

        if universe and contexts:
            print(f"\n  Top Perps by 24h Volume:")
            print(f"  {'Coin':<10} {'24h Vol ($)':<18} {'Open Interest':<18} {'Mark Px':<14} {'Funding':<12}")
            print(f"  {'-'*10} {'-'*18} {'-'*18} {'-'*14} {'-'*12}")

            combined = []
            for i, ctx in enumerate(contexts):
                if i < len(universe):
                    name = universe[i].get("name", f"Coin#{i}")
                    vol = ctx.get("dayNtlVlm", "0")
                    oi = ctx.get("openInterest", "0")
                    mark = ctx.get("markPx", "?")
                    funding = ctx.get("funding", "?")
                    try:
                        vol_f = float(vol)
                    except:
                        vol_f = 0
                    combined.append((name, vol_f, oi, mark, funding))

            combined.sort(key=lambda x: x[1], reverse=True)
            for name, vol_f, oi, mark, funding in combined[:15]:
                try:
                    vol_str = f"${vol_f:>,.0f}"
                except:
                    vol_str = "?"
                try:
                    oi_str = f"${float(oi):>,.0f}"
                except:
                    oi_str = str(oi)
                try:
                    fund_str = f"{float(funding)*100:.4f}%"
                except:
                    fund_str = str(funding)
                print(f"  {name:<10} {vol_str:<18} {oi_str:<18} {mark:<14} {fund_str:<12}")
    print()


# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":
    print("=" * 70)
    print(f"  🔍 FIND BEST FUTURES TRADERS - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    # 1. Try the leaderboard (likely fails - it's frontend-only)
    hl_lb = try_hl_leaderboard()

    # 2. Network stats + top markets
    get_hl_stats()

    # 3. Top Vaults (these always work!)
    get_hl_top_vaults()

    # 4. Inspect known top traders' live positions
    active_whales = inspect_known_whales()

    # 5. Portfolio performance for active traders
    if active_whales:
        print("\n" + "=" * 70)
        print("  HYPERLIQUID - Portfolio Performance (Top Active Traders)")
        print("=" * 70)
        for trader in active_whales[:3]:
            get_hl_portfolio(trader["address"], label=trader["name"])

    # 6. Copin.io multi-chain
    get_copin_top_traders()

    # 7. Summary
    print("\n" + "=" * 70)
    print("  📋 SUMMARY - Best Platforms to Find & Copy Top Futures Traders")
    print("=" * 70)
    print("""
  ┌─────────────────────────────────────────────────────────────────┐
  │ 1. HYPERLIQUID VAULTS                                          │
  │    https://app.hyperliquid.xyz/vaults                          │
  │    ✅ Deposit USDC directly into top traders' vaults            │
  │    ✅ No copy-bot needed - vault does it for you                │
  │    ✅ Transparent on-chain PnL and APR                          │
  │    ✅ BEST for passive investment                                │
  ├─────────────────────────────────────────────────────────────────┤
  │ 2. COPIN.IO                                                    │
  │    https://app.copin.io/explorer                               │
  │    ✅ Aggregates 50+ perp DEXes (GMX, dYdX, Kwenta, HL...)     │
  │    ✅ 2M+ trader profiles with rich analytics                   │
  │    ✅ Built-in copy trading to BingX, Bitget, Gate              │
  │    ✅ BEST for discovering & copying traders across chains       │
  ├─────────────────────────────────────────────────────────────────┤
  │ 3. HYPERLIQUID API (build your own copy-bot)                   │
  │    POST https://api.hyperliquid.xyz/info                       │
  │    ✅ 100% on-chain, fully transparent, free public API         │
  │    ✅ Real-time positions, fills, funding for ANY address        │
  │    ✅ Use clearinghouseState + WebSocket for live monitoring     │
  │    ✅ BEST for building custom copy-trading bots                 │
  ├─────────────────────────────────────────────────────────────────┤
  │ 4. HYPERLIQUID LEADERBOARD                                     │
  │    https://app.hyperliquid.xyz/leaderboard                     │
  │    ✅ Official PnL rankings (daily/weekly/monthly/all-time)     │
  │    ⚠️  Frontend-only (no public API endpoint)                   │
  │    ✅ Get addresses from website → query positions via API       │
  └─────────────────────────────────────────────────────────────────┘

  🔑 RECOMMENDED STRATEGY:
     1. Browse Hyperliquid leaderboard or Copin.io to find top addresses
     2. Use the Hyperliquid public API to monitor their positions in real-time  
     3. Build a copy-bot that mirrors their trades on Hyperliquid or KuCoin
     4. Or simply deposit into a profitable Hyperliquid Vault
    """)

    print("=" * 70)
    print("  Done!")
    print("=" * 70)
