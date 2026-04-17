"""
Test KuCoin API access for Copy Trading endpoints.
Tests authentication and checks access to copy trading trader endpoints.
"""

import hashlib
import hmac
import base64
import time
import json
import urllib.request
import urllib.error

# KuCoin API credentials — set via environment variables; never commit real keys.
import os
API_KEY = os.getenv("KUCOIN_API_KEY", "")
API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")
if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
    raise SystemExit("Set KUCOIN_API_KEY, KUCOIN_API_SECRET, KUCOIN_API_PASSPHRASE before running.")

# Base URLs
SPOT_BASE_URL = "https://api.kucoin.com"
FUTURES_BASE_URL = "https://api-futures.kucoin.com"


def generate_signature(secret, timestamp, method, endpoint, body=""):
    """Generate KC-API-SIGN header."""
    str_to_sign = str(timestamp) + method.upper() + endpoint + body
    signature = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            str_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return signature


def generate_passphrase(secret, passphrase):
    """Generate KC-API-PASSPHRASE header (HMAC-encrypted for v2)."""
    return base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            passphrase.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")


def make_request(base_url, method, endpoint, body=""):
    """Make an authenticated request to KuCoin API."""
    timestamp = str(int(time.time() * 1000))
    signature = generate_signature(API_SECRET, timestamp, method, endpoint, body)
    passphrase = generate_passphrase(API_SECRET, API_PASSPHRASE)

    headers = {
        "KC-API-KEY": API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }

    url = base_url + endpoint
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode("utf-8"))
            return {"status": response.status, "data": result}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            error_json = json.loads(error_body)
        except json.JSONDecodeError:
            error_json = error_body
        return {"status": e.code, "error": error_json}
    except Exception as e:
        return {"status": None, "error": str(e)}


def print_result(label, result):
    """Pretty print API result."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  HTTP Status: {result.get('status')}")
    if "data" in result:
        print(f"  Response: {json.dumps(result['data'], indent=4)}")
    elif "error" in result:
        print(f"  Error: {json.dumps(result['error'], indent=4) if isinstance(result['error'], (dict, list)) else result['error']}")
    print()


if __name__ == "__main__":
    print("=" * 60)
    print("  KuCoin API - Copy Trading Access Test")
    print("=" * 60)

    # 1. Test basic auth by getting API key info
    print("\n[1] Testing basic API authentication...")
    result = make_request(SPOT_BASE_URL, "GET", "/api/v1/user/api-key")
    print_result("API Key Info", result)
    if "data" in result and "data" in result["data"]:
        perms = result["data"]["data"].get("permission", "")
        print(f"  >> Permissions: {perms}")
        has_lead = "LeadtradeFutures" in perms
        print(f"  >> Has LeadtradeFutures permission: {has_lead}")
        if not has_lead:
            print("  >> ⚠️  Copy Trading (Lead Trader) requires 'LeadtradeFutures' permission on the API key!")
    print()

    # ALL copy trading endpoints use api.kucoin.com (not api-futures!)
    # Correct paths from OpenAPI spec docs
    BASE = SPOT_BASE_URL

    # 2. Copy Trading - Get Max Open Size (requires: symbol, price, leverage)
    print("[2] Testing Copy Trading - Get Max Open Size...")
    result = make_request(
        BASE, "GET",
        "/api/v1/copy-trade/futures/get-max-open-size?symbol=XBTUSDTM&price=50000&leverage=5"
    )
    print_result("Copy Trading - Get Max Open Size", result)

    # 3. Copy Trading - Get Max Withdraw Margin
    print("[3] Testing Copy Trading - Get Max Withdraw Margin...")
    result = make_request(
        BASE, "GET",
        "/api/v1/copy-trade/futures/get-max-withdraw-margin?symbol=XBTUSDTM"
    )
    print_result("Copy Trading - Get Max Withdraw Margin", result)

    # 4. Copy Trading - Get Cross Margin Requirement
    print("[4] Testing Copy Trading - Get Cross Margin Requirement...")
    result = make_request(
        BASE, "GET",
        "/api/v1/copy-trade/futures/get-cross-margin-requirement?symbol=XBTUSDTM"
    )
    print_result("Copy Trading - Get Cross Margin Requirement", result)

    # 5. Try on api-futures.kucoin.com as well (Classic Account Futures domain)
    print("[5] Testing copy trade on FUTURES domain (api-futures.kucoin.com)...")
    futures_eps = [
        "/api/v1/copy-trade/futures/get-max-open-size?symbol=XBTUSDTM&price=50000&leverage=5",
        "/api/v1/copy-trade/futures/orders",
        "/api/v1/copy-trade/futures/positions",
    ]
    for ep in futures_eps:
        result = make_request(FUTURES_BASE_URL, "GET", ep)
        status = result.get("status")
        code = None
        if "data" in result:
            code = result["data"].get("code")
        elif "error" in result and isinstance(result["error"], dict):
            code = result["error"].get("code")
        if status != 404 and code != "404":
            print_result(f"  FUTURES: {ep}", result)
        else:
            print(f"  FUTURES: {ep} -> 404 Not Found")

    # 6. Broad endpoint scan for any copy-trade related paths
    print("\n[6] Exhaustive copy-trade endpoint scan (both domains)...")
    all_eps = [
        "/api/v1/copy-trade/futures/get-max-open-size?symbol=XBTUSDTM&price=50000&leverage=5",
        "/api/v1/copy-trade/futures/get-max-withdraw-margin?symbol=XBTUSDTM",
        "/api/v1/copy-trade/futures/get-cross-margin-requirement?symbol=XBTUSDTM",
        "/api/v1/copy-trade/futures/switch-margin-mode",
        "/api/v1/copy-trade/futures/modify-cross-margin-leverage",
        "/api/v1/copy-trade/futures/switch-position-mode",
        "/api/v1/copy-trade/futures/add-isolated-margin",
        "/api/v1/copy-trade/futures/remove-isolated-margin",
        "/api/v1/copy-trade/futures/modify-isolated-margin-risk-limit",
        "/api/v3/copy-trade/futures/get-max-open-size?symbol=XBTUSDTM&price=50000&leverage=5",
    ]
    for base_label, base_url in [("SPOT", SPOT_BASE_URL), ("FUTURES", FUTURES_BASE_URL)]:
        for ep in all_eps:
            result = make_request(base_url, "GET", ep)
            status = result.get("status")
            code = None
            if "data" in result:
                code = result["data"].get("code")
            elif "error" in result and isinstance(result["error"], dict):
                code = result["error"].get("code")
            if status != 404 and code != "404":
                print_result(f"  ✅ {base_label}: {ep}", result)
            else:
                print(f"  ❌ {base_label}: {ep} -> 404")

    print()
    print("=" * 60)
    print("  Test Complete")
    print("=" * 60)
