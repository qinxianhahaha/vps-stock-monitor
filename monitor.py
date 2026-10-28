#!/usr/bin/env python3
"""
Lightweight changedetection.io-style VPS stock monitor for BandwagonHost & DMIT.
Supports both daemon mode (--interval N) and single-shot mode (--once, e.g. in GitHub Actions).
Inspects specific DOM selectors/keywords, retries on transient SSL/network errors,
distinguishes between 'out_of_stock', 'invite_required', and genuinely 'in_stock'
(purchasable without invite code), writes state to status.json, logs changes to changes.log,
and triggers the Grok Bot automation webhook when a plan is genuinely in_stock.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

DEFAULT_BASE_DIR = "/workspace/vps-monitor" if os.path.isdir("/workspace") else os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("VPS_MONITOR_DIR", DEFAULT_BASE_DIR)
STATUS_FILE = os.path.join(BASE_DIR, "status.json")
CHANGES_LOG = os.path.join(BASE_DIR, "changes.log")
WEBHOOK_CONFIG_FILE = os.path.join(BASE_DIR, "webhook.json")
TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

PLANS = [
    {
        "id": "bwh_157",
        "vendor": "BandwagonHost",
        "pid": "157",
        "name": "MEGABOX-PRO",
        "price": "$49/yr",
        "cart_url": "https://bwh81.net/cart.php?a=add&pid=157",
    },
    {
        "id": "bwh_156",
        "vendor": "BandwagonHost",
        "pid": "156",
        "name": "BIGGERBOX-PRO",
        "price": "$39/yr",
        "cart_url": "https://bwh81.net/cart.php?a=add&pid=156",
    },
    {
        "id": "bwh_94",
        "vendor": "BandwagonHost",
        "pid": "94",
        "name": "CN2 GIA-E 10G",
        "price": "$49.99/yr",
        "cart_url": "https://bwh81.net/cart.php?a=add&pid=94",
    },
    {
        "id": "bwh_147",
        "vendor": "BandwagonHost",
        "pid": "147",
        "name": "THE PLAN 2024",
        "price": "$99/yr",
        "cart_url": "https://bwh81.net/cart.php?a=add&pid=147",
    },
    {
        "id": "dmit_183",
        "vendor": "DMIT",
        "pid": "183",
        "name": "LAX.Pro.WEE",
        "price": "$39.90/yr",
        "cart_url": "https://www.dmit.io/cart.php?a=add&pid=183",
    },
    {
        "id": "dmit_186",
        "vendor": "DMIT",
        "pid": "186",
        "name": "LAX.Pro.MALIBU",
        "price": "$49.90/yr",
        "cart_url": "https://www.dmit.io/cart.php?a=add&pid=186",
    },
    {
        "id": "dmit_218",
        "vendor": "DMIT",
        "pid": "218",
        "name": "LAX.EB.CORONA",
        "price": "$49.90/yr",
        "cart_url": "https://www.dmit.io/cart.php?a=add&pid=218",
    },
    {
        "id": "dmit_188",
        "vendor": "DMIT",
        "pid": "188",
        "name": "LAX.EB.WEE",
        "price": "$39.90/yr",
        "cart_url": "https://www.dmit.io/cart.php?a=add&pid=188",
    },
]

STOCK_BWH_URL = "https://stock.bwh91.com/"
STOCK_DMIT_URL = "https://stock.dmitea.com/"


def now_shanghai_str() -> str:
    return datetime.datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %Z (UTC+8)")


def request_with_retry(method: str, url: str, session=None, retries: int = 2, backoff: float = 1.5, **kwargs):
    """
    Perform an HTTP request with retry and exponential backoff on transient SSL/network errors.
    """
    requester = session if session is not None else requests
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return requester.request(method, url, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    raise last_exc


def parse_third_party_table(url: str):
    """
    Fetch and parse stock.bwh91.com or stock.dmitea.com.
    Returns dict mapping pid -> {status, raw_status, title, upstream_checked_at} and promo_code.
    """
    result = {"ok": False, "plans": {}, "promo_code": None, "error": None}
    try:
        resp = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        html = resp.content.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")

        # Extract promo code from script or .coupon-code element
        m = re.search(r"const\s+couponCode\s*=\s*['\"]([^'\"]+)['\"]", html)
        if m:
            result["promo_code"] = m.group(1).strip()
        else:
            coupon_el = soup.select_one(".coupon-code")
            if coupon_el:
                result["promo_code"] = coupon_el.get_text(" ", strip=True)

        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            a = tr.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            pid_match = re.search(r"pid=(\d+)", href)
            if not pid_match:
                continue
            pid = pid_match.group(1)
            title_text = tds[0].get_text(" ", strip=True)
            stock_text = tds[1].get_text(" ", strip=True)
            checked_at = tds[3].get_text(" ", strip=True)

            if "无货" in stock_text:
                norm_status = "out_of_stock"
            elif "有货" in stock_text:
                if "邀请码" in title_text or "邀请码" in stock_text:
                    norm_status = "invite_required"
                else:
                    norm_status = "in_stock"
            else:
                norm_status = "unknown"

            result["plans"][pid] = {
                "status": norm_status,
                "raw_status": stock_text,
                "title": title_text,
                "upstream_checked_at": checked_at,
            }
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def check_bwh_direct(cart_url: str):
    """
    Directly check BandwagonHost WHMCS cart URL using a cookie-enabled Session.
    DOM region inspected: #order-web20cart (and #whmcsorderfrm).
    Distinguishes:
      - 'out_of_stock': .errorbox or #order-web20cart contains 'Out of Stock'
      - 'invite_required': confproduct or cart.php?a=view requires an Invite Code
      - 'in_stock': reaches Product Configuration & Order Summary without Invite Code requirement
    """
    info = {
        "direct_ok": False,
        "status": "unknown",
        "http_status": None,
        "final_url": None,
        "dom_summary": None,
        "easter_promo": None,
        "error": None,
    }
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        resp = request_with_retry("GET", cart_url, session=session, timeout=15)
        info["http_status"] = resp.status_code
        info["final_url"] = resp.url
        soup = BeautifulSoup(resp.text, "html.parser")
        order_div = soup.find(id="order-web20cart") or soup.find(id="whmcsorderfrm")
        if not order_div:
            info["error"] = "Missing #order-web20cart DOM container"
            return info

        text = order_div.get_text(" ", strip=True)
        info["dom_summary"] = text[:200]
        info["direct_ok"] = True

        # Check easter promo code if present on confproduct page
        easter_div = order_div.find(id="easter")
        if easter_div:
            easter_txt = easter_div.get_text(" ", strip=True)
            m = re.search(r"Try this promo code:\s*([A-Za-z0-9_-]+)", easter_txt)
            if m:
                info["easter_promo"] = m.group(1)

        error_box = order_div.find(class_="errorbox")
        if (error_box and "out of stock" in error_box.get_text(" ", strip=True).lower()) or (
            "out of stock" in text.lower()
        ):
            info["status"] = "out_of_stock"
            return info

        if "invite code required" in text.lower():
            info["status"] = "invite_required"
            return info

        # If on Product Configuration page, verify via anonymous cart view step
        # whether an Invite Code is required at checkout summary (e.g. pid=159 behavior)
        form = order_div.find("form", action=lambda x: x and "confproduct" in x)
        if form:
            post_data = {}
            for inp in form.find_all(["input", "select"]):
                name = inp.get("name")
                if not name:
                    continue
                if inp.name == "select":
                    opt = inp.find("option", selected=True) or inp.find("option")
                    if opt:
                        post_data[name] = opt.get("value", "")
                else:
                    post_data[name] = inp.get("value", "")
            r2 = request_with_retry(
                "POST",
                "https://bwh81.net" + form["action"],
                session=session,
                data=post_data,
                timeout=15,
            )
            soup2 = BeautifulSoup(r2.text, "html.parser")
            view_div = soup2.find(id="order-web20cart") or soup2.find(id="whmcsorderfrm")
            view_text = view_div.get_text(" ", strip=True) if view_div else r2.text
            info["final_url"] = r2.url
            info["dom_summary"] = view_text[:200]
            if "invite code required" in view_text.lower():
                info["status"] = "invite_required"
            elif "order summary" in view_text.lower() or "total due today" in view_text.lower():
                info["status"] = "in_stock"
            else:
                info["status"] = "unknown"
            return info

        if "product configuration" in text.lower():
            info["status"] = "in_stock"
        return info
    except Exception as e:
        info["error"] = str(e)
        return info


def check_dmit_direct(cart_url: str):
    """
    Attempt direct GET to www.dmit.io cart URL.
    Note: www.dmit.io enforces Cloudflare Managed Challenge (HTTP 403) on datacenter IPs,
    so when 403 is returned we record 'cloudflare_blocked' and rely on stock.dmitea.com.
    """
    info = {
        "direct_ok": False,
        "status": "cloudflare_blocked",
        "http_status": None,
        "error": None,
    }
    try:
        resp = request_with_retry("GET", cart_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        info["http_status"] = resp.status_code
        if resp.status_code == 403 or "Attention Required! | Cloudflare" in resp.text:
            info["status"] = "cloudflare_blocked"
            info["error"] = "HTTP 403 Cloudflare Managed Challenge"
            return info
        info["direct_ok"] = True
        lower = resp.text.lower()
        if "out of stock" in lower:
            info["status"] = "out_of_stock"
        elif "product configuration" in lower or "configure" in lower:
            info["status"] = "in_stock"
        else:
            info["status"] = "unknown"
        return info
    except Exception as e:
        info["error"] = str(e)
        return info


def append_change_log(message: str):
    ts = now_shanghai_str()
    line = f"[{ts}] {message}\n"
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(CHANGES_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


def trigger_webhook_if_configured(restock_events, snapshot):
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    webhook_key = os.environ.get("WEBHOOK_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if webhook_key:
        if webhook_key.lower().startswith("bearer "):
            headers["Authorization"] = webhook_key
        else:
            headers["Authorization"] = f"Bearer {webhook_key}"

    if (not webhook_url or "Authorization" not in headers) and os.path.exists(WEBHOOK_CONFIG_FILE):
        try:
            with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not webhook_url:
                webhook_url = cfg.get("url", "").strip()
            if "Authorization" not in headers:
                if isinstance(cfg.get("headers"), dict) and cfg["headers"].get("Authorization"):
                    headers.update(cfg["headers"])
                elif cfg.get("key"):
                    headers["Authorization"] = f"Bearer {cfg['key'].strip()}"
        except Exception as e:
            append_change_log(f"WARN: Failed reading {WEBHOOK_CONFIG_FILE}: {e}")

    if not webhook_url:
        append_change_log("WARN: Restock detected but WEBHOOK_URL is not configured.")
        return

    payload = {
        "event": "vps_restock_detected",
        "checked_at": snapshot["last_checked_at"],
        "restock_events": restock_events,
        "promo_code": snapshot.get("bwh_promo_code"),
    }
    try:
        r = request_with_retry("POST", webhook_url, headers=headers, json=payload, timeout=15)
        append_change_log(f"WEBHOOK triggered ({r.status_code}): {webhook_url}")
    except Exception as e:
        append_change_log(f"ERROR triggering webhook {webhook_url}: {e}")


def run_check(previous_state: dict) -> dict:
    checked_at = now_shanghai_str()
    bwh_table = parse_third_party_table(STOCK_BWH_URL)
    dmit_table = parse_third_party_table(STOCK_DMIT_URL)

    prev_plans = previous_state.get("plans", {}) if previous_state else {}
    plans_status = {}
    restock_events = []
    status_changes = []
    discovered_easter_promo = None

    for plan in PLANS:
        plan_id = plan["id"]
        pid = plan["pid"]
        vendor = plan["vendor"]

        if vendor == "BandwagonHost":
            direct = check_bwh_direct(plan["cart_url"])
            if direct.get("easter_promo"):
                discovered_easter_promo = direct["easter_promo"]
            tp_entry = bwh_table["plans"].get(pid, {})
            # Primary authority for BWH is direct WHMCS cart inspection; fallback to stock.bwh91.com
            if direct["direct_ok"] and direct["status"] != "unknown":
                effective_status = direct["status"]
                authority = "direct_whmcs_cart"
            else:
                effective_status = tp_entry.get("status", "unknown")
                authority = "stock.bwh91.com"
        else:
            direct = check_dmit_direct(plan["cart_url"])
            tp_entry = dmit_table["plans"].get(pid, {})
            # Primary authority for DMIT is stock.dmitea.com (since direct URL hits Cloudflare 403)
            if direct["direct_ok"] and direct["status"] not in ("unknown", "cloudflare_blocked"):
                effective_status = direct["status"]
                authority = "direct_whmcs_cart"
            else:
                effective_status = tp_entry.get("status", "unknown")
                authority = "stock.dmitea.com"

        purchasable_without_invite = effective_status == "in_stock"

        prev_entry = prev_plans.get(plan_id)
        prev_status = prev_entry.get("status") if prev_entry else None

        if prev_status is not None and prev_status != effective_status:
            change_obj = {
                "plan_id": plan_id,
                "vendor": vendor,
                "name": plan["name"],
                "pid": pid,
                "price": plan["price"],
                "cart_url": plan["cart_url"],
                "old_status": prev_status,
                "new_status": effective_status,
                "detected_at": checked_at,
            }
            status_changes.append(change_obj)
            append_change_log(
                f"STATUS CHANGE: {vendor} {plan['name']} (pid={pid}) "
                f"{prev_status} -> {effective_status} ({plan['cart_url']})"
            )

        if purchasable_without_invite and prev_status != "in_stock":
            restock_obj = {
                "plan_id": plan_id,
                "vendor": vendor,
                "name": plan["name"],
                "pid": pid,
                "price": plan["price"],
                "cart_url": plan["cart_url"],
                "detected_at": checked_at,
                "authority": authority,
            }
            restock_events.append(restock_obj)
            append_change_log(
                f"RESTOCK ALERT (NO INVITE CODE REQUIRED): {vendor} {plan['name']} "
                f"({plan['price']}, pid={pid}) is IN STOCK! URL: {plan['cart_url']}"
            )

        plans_status[plan_id] = {
            "vendor": vendor,
            "pid": pid,
            "name": plan["name"],
            "price": plan["price"],
            "cart_url": plan["cart_url"],
            "status": effective_status,
            "purchasable_without_invite": purchasable_without_invite,
            "authority": authority,
            "direct_check": direct,
            "third_party_check": tp_entry,
        }

    raw_bwh_promo = bwh_table.get("promo_code")
    fallback_promo = dmit_table.get("promo_code")
    active_promo = discovered_easter_promo
    if not active_promo and raw_bwh_promo and raw_bwh_promo not in ("暂缺", "无", "None"):
        active_promo = raw_bwh_promo

    snapshot = {
        "last_checked_at": checked_at,
        "poll_interval_seconds": 60,
        "bwh_promo_code": {
            "active_code": active_promo,
            "stock_bwh91_raw": raw_bwh_promo,
            "historical_fallback_code": fallback_promo or "BWHCGLUKKB",
            "note": (
                "stock.bwh91.com currently reports '暂缺' (BWH recurring promo codes paused); "
                "historical 6.77% code is BWHCGLUKKB."
                if raw_bwh_promo == "暂缺" and not active_promo
                else "Active promo code extracted."
            ),
        },
        "sources_health": {
            "bwh_direct_cart": {
                "ok": all(
                    plans_status[k]["direct_check"]["direct_ok"]
                    for k in ("bwh_157", "bwh_156", "bwh_94", "bwh_147")
                ),
                "method": "requests.Session() with cookies + retry -> #order-web20cart DOM & cart.php?a=view invite check",
            },
            "dmit_direct_cart": {
                "ok": False,
                "method": "Blocked by Cloudflare Managed Challenge (HTTP 403); using stock.dmitea.com as authority",
            },
            "stock_bwh91_com": {
                "ok": bwh_table["ok"],
                "error": bwh_table["error"],
            },
            "stock_dmitea_com": {
                "ok": dmit_table["ok"],
                "error": dmit_table["error"],
            },
        },
        "in_stock_count": sum(1 for p in plans_status.values() if p["purchasable_without_invite"]),
        "restock_events": restock_events,
        "status_changes": status_changes,
        "plans": plans_status,
    }

    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        tmp_file = STATUS_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, STATUS_FILE)
    except Exception:
        pass

    if not previous_state:
        append_change_log(
            f"INITIAL CHECK COMPLETED: {len(plans_status)} plans checked, "
            f"in_stock_count={snapshot['in_stock_count']}."
        )

    if restock_events:
        trigger_webhook_if_configured(restock_events, snapshot)

    return snapshot


def load_existing_state() -> dict:
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def main():
    parser = argparse.ArgumentParser(description="VPS stock monitor daemon")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    args = parser.parse_args()

    os.makedirs(BASE_DIR, exist_ok=True)
    state = load_existing_state()

    while True:
        try:
            state = run_check(state)
            print(
                f"[{state['last_checked_at']}] Check OK | in_stock={state['in_stock_count']}/8",
                flush=True,
            )
        except Exception as e:
            append_change_log(f"ERROR in monitor loop: {e}")

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
