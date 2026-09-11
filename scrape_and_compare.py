#!/usr/bin/env python3
"""
Guldcentrum.se + Guldexperten.se price tracker -> Discord webhook notifier.

Scrapes the buy-price tables from both sites, maps their (differently
worded) karat labels onto a shared set of categories, and posts a
side-by-side comparison to Discord whenever either site's prices change.
Also flags any karat where Guldexperten is priced higher than Guldcentrum
(e.g. "Guldexperten 18K +3 kr").

Setup:
  1. pip install -r requirements.txt
  2. export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
  3. python3 scrape_and_compare.py

First run has no history, so it posts the full comparison once as a
baseline, then only posts again when a price actually changes.
"""

import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

GULDCENTRUM_URL = "https://www.guldcentrum.se"
# This is the iframe's actual src -- fetch it directly, no need to load the
# guldexperten.se page that embeds it.
GULDEXPERTEN_URL = "https://www.guldexperten.se/goldprice/showPrice.php"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_prices.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ---------------------------------------------------------------------------
# Canonical karat categories both sites get mapped onto.
# Guldcentrum has no 20K row; Guldexperten does -- that's fine, it'll just
# show as "-" on the Guldcentrum side.
# ---------------------------------------------------------------------------
CANONICAL_ORDER = ["24K", "23K", "22K", "21K", "20K", "18K", "14K", "9K", "Tandguld", "Silver 999"]

DISPLAY_NAMES = {
    "24K": "24K",
    "23K": "23K",
    "22K": "22K",
    "21K": "21K",
    "20K": "20K",
    "18K": "18K",
    "14K": "14K",
    "9K": "9K",
    "Tandguld": "Tandguld",
    "Silver 999": "Silver 999",
}

GULDCENTRUM_LABEL_MAP = {
    "Investeringsguld 999.9/24K Nypräglad": "24K",
    "Guld 23K": "23K",
    "Investeringsguld 22K Mynt": "22K",
    "Guld 21k": "21K",
    "Guld 18K": "18K",
    "Guld 14K": "14K",
    "Tandguld Guldhalt 750": "Tandguld",
    "Guld 9K": "9K",
    "Silver 999 Nypräglad": "Silver 999",
}

GULDEXPERTEN_LABEL_MAP = {
    "24K nypräglade tackor": "24K",
    "23K": "23K",
    "22K investeringsguld": "22K",
    "21K": "21K",
    "20K": "20K",
    "18K": "18K",
    "14K": "14K",
    "9K": "9K",
    "TANDGULD": "Tandguld",
    "999 SILVER Nypräglad": "Silver 999",
}


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_guldcentrum_html(url: str) -> str:
    """
    Guldcentrum.se renders its price table with JavaScript after the page
    loads, so a plain `requests` fetch only sees an empty shell. Use a
    real (headless) browser via Playwright instead, so the JS actually runs.

    Note: the page's title ("Inköpspriser") renders immediately, but the
    actual price values arrive slightly later via a separate backend call.
    So we wait for an actual price cell, not just the title, to avoid
    grabbing the HTML before the numbers have loaded.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(".table-wrap .text-right.table-item", timeout=25000)
            page.wait_for_timeout(500)  # small buffer in case more rows are still landing
            html = page.content()
        finally:
            browser.close()
        return html


def get_guldcentrum_prices(html: str) -> dict:
    """Parse the 'Inköpspriser' (Vi köper) table -> {raw label: value_str}."""
    soup = BeautifulSoup(html, "html.parser")
    raw = {}
    for card in soup.select(".calculator-card-content"):
        title_el = card.select_one(".calculator-card-title")
        if not title_el or "Inköpspriser" not in title_el.get_text(strip=True):
            continue
        for wrap in card.select(".table-wrap"):
            label_el = wrap.select_one(".table-item.text-left")
            value_el = wrap.select_one(".table-item.text-right")
            if label_el and value_el:
                raw[label_el.get_text(strip=True)] = value_el.get_text(strip=True).replace("\xa0", " ")
    return raw


def get_guldexperten_prices(html: str) -> dict:
    """Parse the iframe table -> {raw label: value_str}."""
    soup = BeautifulSoup(html, "html.parser")
    raw = {}
    for row in soup.select(".name-price-desc"):
        name_el = row.select_one(".name.a-tag")
        price_el = row.select_one(".spl-price.a-tag")
        if name_el and price_el:
            raw[name_el.get_text(strip=True)] = price_el.get_text(strip=True)
    return raw


def normalize(raw: dict, label_map: dict, site_name: str) -> dict:
    """Map a site's raw labels onto canonical karat keys."""
    canon = {}
    for label, value in raw.items():
        key = label_map.get(label)
        if key is None:
            print(f"[{site_name}] Unmapped label {label!r} -- add it to the label map. Skipping.", file=sys.stderr)
            continue
        canon[key] = value
    return canon


def _to_float(value_str: str):
    """'1 341,00 kr/g' or '1340 Kr/g' -> 1341.0 / 1340.0"""
    cleaned = re.sub(r"[^\d,.\-]", "", value_str)
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def load_last() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                return {}
            # Guard against an old, differently-shaped state file.
            if "guldcentrum" in data or "guldexperten" in data:
                return data
    return {}


def save_current(gc: dict, ge: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"guldcentrum": gc, "guldexperten": ge}, f, ensure_ascii=False, indent=2)


def diff(last_site: dict, current_site: dict) -> dict:
    changes = {}
    for key, value in current_site.items():
        if key in last_site and last_site[key] != value:
            changes[key] = (last_site[key], value)
    return changes


def build_embed(gc: dict, ge: dict, changes_gc: dict, changes_ge: dict, first_run: bool) -> dict:
    """
    Build a Discord embed (card) instead of a plain code-block table.
    Embeds stack their fields vertically on mobile automatically, so
    nothing gets cut off or forces horizontal scrolling on a phone screen
    the way a fixed-width monospace table would.
    """
    higher_ge = []  # (karat, diff_kr)
    higher_gc = []
    fields = []

    for key in CANONICAL_ORDER:
        gc_val = gc.get(key)
        ge_val = ge.get(key)
        if gc_val is None and ge_val is None:
            continue

        lines = []
        gc_marker = " 🔄" if key in changes_gc else ""
        ge_marker = " 🔄" if key in changes_ge else ""
        lines.append(f"Guldcentrum: {gc_val}{gc_marker}" if gc_val else "Guldcentrum: –")
        lines.append(f"Guldexperten: {ge_val}{ge_marker}" if ge_val else "Guldexperten: –")

        gc_f = _to_float(gc_val) if gc_val else None
        ge_f = _to_float(ge_val) if ge_val else None
        if gc_f is not None and ge_f is not None and gc_f != ge_f:
            d = round(ge_f - gc_f)
            if d > 0:
                lines.append(f"⬆️ Guldexperten +{d} kr")
                higher_ge.append((key, d))
            else:
                lines.append(f"⬆️ Guldcentrum +{-d} kr")
                higher_gc.append((key, -d))

        was_changed = key in changes_gc or key in changes_ge
        fields.append({
            "name": f"{'🔔 ' if was_changed else ''}{DISPLAY_NAMES[key]}",
            "value": "\n".join(lines),
            # inline=False forces one karat per row -- guarantees no
            # cramped side-by-side columns on narrow phone screens.
            "inline": False,
        })

    description_parts = []
    if higher_ge:
        description_parts.append(
            "**Guldexperten dyrare:** " + ", ".join(f"{DISPLAY_NAMES[k]} +{d}kr" for k, d in higher_ge)
        )
    if higher_gc:
        description_parts.append(
            "**Guldcentrum dyrare:** " + ", ".join(f"{DISPLAY_NAMES[k]} +{d}kr" for k, d in higher_gc)
        )

    embed = {
        "title": "🪙 Guldpriser uppdaterade" if not first_run else "🪙 Guldpriser (baslinje)",
        "color": 0xD4AF37,  # gold
        "fields": fields,
        "footer": {"text": "🔄 = ändrat sedan senaste körningen"},
    }
    if description_parts:
        embed["description"] = "\n".join(description_parts)

    return embed


def send_discord(embed: dict) -> None:
    payload = {"embeds": [embed]}
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set. Would have sent:\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    resp.raise_for_status()


def main() -> int:
    last = load_last()
    first_run = not last

    # --- Guldcentrum: Playwright/browser-based, the more failure-prone
    # fetch, especially when run in launchd's background session rather
    # than an interactive Terminal. Retry a few times with a pause between
    # attempts; if it still fails, fall back to the last known-good prices
    # rather than sending a broken "all dashes" comparison or wiping history.
    gc_raw = {}
    gc_failed = False
    for attempt in range(1, 4):
        try:
            gc_html = fetch_guldcentrum_html(GULDCENTRUM_URL)
            gc_raw = get_guldcentrum_prices(gc_html)
            if gc_raw:
                break
            print(f"Guldcentrum prices empty on attempt {attempt}/3, retrying...", file=sys.stderr)
        except Exception as e:
            print(f"Guldcentrum fetch failed on attempt {attempt}/3: {e}", file=sys.stderr)
        time.sleep(5)

    if not gc_raw:
        gc_failed = True
        print("Guldcentrum unreachable this run -- falling back to last known prices.", file=sys.stderr)

    # --- Guldexperten: plain requests, has been reliable so far ---
    try:
        ge_html = fetch_html(GULDEXPERTEN_URL)
        ge_raw = get_guldexperten_prices(ge_html)
    except Exception as e:
        print(f"Guldexperten fetch failed: {e}", file=sys.stderr)
        ge_raw = {}

    if not gc_raw and not ge_raw and not last:
        print("No prices found on either site, and no prior history to fall back on.", file=sys.stderr)
        return 1

    gc_new = normalize(gc_raw, GULDCENTRUM_LABEL_MAP, "guldcentrum") if gc_raw else {}
    ge = normalize(ge_raw, GULDEXPERTEN_LABEL_MAP, "guldexperten")

    # Use freshly scraped Guldcentrum data if we got it; otherwise reuse
    # whatever we last successfully saved, so the comparison table and
    # history stay intact through a flaky run.
    gc = gc_new if gc_new else last.get("guldcentrum", {})

    changes_gc = diff(last.get("guldcentrum", {}), gc_new) if gc_new else {}
    changes_ge = diff(last.get("guldexperten", {}), ge)

    if changes_gc or changes_ge or first_run:
        embed = build_embed(gc, ge, changes_gc, changes_ge, first_run)
        if gc_failed:
            note = "⚠️ Guldcentrum kunde inte hämtas denna gång -- visar senast kända pris."
            embed["description"] = (embed.get("description", "") + "\n\n" + note).strip()
        send_discord(embed)
        print("Sent Discord update." if not first_run else "Sent initial baseline to Discord.")
    else:
        print("No price changes on either site.")

    save_current(gc, ge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
