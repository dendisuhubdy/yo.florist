#!/usr/bin/env python3
"""Daily catalog sync for yo.florist.

Visits every partner florist's website and extracts bouquet products
(title, price, currency, image, product URL) into a catalog the API serves.
Runs as a systemd timer (yoflorist-scraper.timer); safe to run by hand:

    python3 /opt/yoflorist/scraper.py

Extraction strategies, tried in order per site:
  1. Shopify storefront JSON  ({base}/products.json + currency from /cart.js)
  2. WooCommerce Store API    ({base}/wp-json/wc/store/v1/products)
  3. schema.org Product JSON-LD on the homepage and obvious shop pages

Sites that are social-media-only or yield no priced products are skipped —
the API falls back to reroute-to-website behaviour for those countries.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

FLORISTS_FILE = Path("/opt/yoflorist/florists.json")
CATALOG_FILE = Path("/opt/yoflorist/data/catalog.json")
UA = "YoFloristBot/1.0 (+https://yo.florist; daily catalog sync; contact: dendi.suhubdy@gmail.com)"
TIMEOUT = 12
MAX_ITEMS = 12
WORKERS = 8

SKIP_HOSTS = ("facebook.com", "instagram.com", "wordpress.com", "yellow.sc", "vanuatu.travel")

session = requests.Session()
session.headers["User-Agent"] = UA


def fetch(url, **kw):
    try:
        r = session.get(url, timeout=TIMEOUT, **kw)
        return r if r.ok else None
    except requests.RequestException:
        return None


def robots_allows(base):
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(urllib.parse.urljoin(base, "/robots.txt"))
        rp.read()
        return rp.can_fetch(UA, urllib.parse.urljoin(base, "/products.json"))
    except Exception:
        return True  # no readable robots.txt → assume allowed


def to_price(val):
    """Normalise a scraped price to float, or None if not parseable."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = re.sub(r"[^\d.,]", "", str(val))
    if not s:
        return None
    # "1.234,56" → 1234.56 ; "1,234.56" → 1234.56 ; "45" → 45.0
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def try_shopify(base):
    r = fetch(urllib.parse.urljoin(base, "/products.json?limit=25"))
    if r is None or "json" not in r.headers.get("content-type", ""):
        return []
    try:
        products = r.json().get("products") or []
    except ValueError:
        return []
    currency = None
    cart = fetch(urllib.parse.urljoin(base, "/cart.js"))
    if cart is not None:
        try:
            currency = cart.json().get("currency")
        except ValueError:
            pass
    items = []
    for p in products:
        price = to_price((p.get("variants") or [{}])[0].get("price"))
        if not p.get("title") or price is None:
            continue
        items.append({
            "title": p["title"].strip(),
            "price": price,
            "currency": currency,
            "image": (p.get("images") or [{}])[0].get("src"),
            "url": urllib.parse.urljoin(base, f"/products/{p.get('handle', '')}"),
        })
    return items


def try_woocommerce(base):
    for path in ("/wp-json/wc/store/v1/products?per_page=25", "/wp-json/wc/store/products?per_page=25"):
        r = fetch(urllib.parse.urljoin(base, path))
        if r is None or "json" not in r.headers.get("content-type", ""):
            continue
        try:
            products = r.json()
        except ValueError:
            continue
        if not isinstance(products, list):
            continue
        items = []
        for p in products:
            prices = p.get("prices") or {}
            minor = int(prices.get("currency_minor_unit", 2))
            raw = prices.get("price")
            price = None if raw is None else to_price(raw)
            if price is not None:
                price = round(price / (10 ** minor), 2)
            if not p.get("name") or price is None:
                continue
            images = p.get("images") or [{}]
            items.append({
                "title": BeautifulSoup(p["name"], "html.parser").get_text().strip(),
                "price": price,
                "currency": prices.get("currency_code"),
                "image": images[0].get("thumbnail") or images[0].get("src"),
                "url": p.get("permalink"),
            })
        if items:
            return items
    return []


def _products_from_jsonld(node, page_url):
    """Recursively pull schema.org Product nodes out of a JSON-LD blob."""
    found = []
    if isinstance(node, list):
        for n in node:
            found.extend(_products_from_jsonld(n, page_url))
    elif isinstance(node, dict):
        types = node.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if "Product" in types:
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = to_price(offers.get("price") or offers.get("lowPrice"))
            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            if node.get("name") and price is not None:
                found.append({
                    "title": str(node["name"]).strip(),
                    "price": price,
                    "currency": offers.get("priceCurrency"),
                    "image": image,
                    "url": node.get("url") or offers.get("url") or page_url,
                })
        for v in node.values():
            if isinstance(v, (dict, list)):
                found.extend(_products_from_jsonld(v, page_url))
    return found


def try_jsonld(base):
    pages = [base]
    r = fetch(base)
    if r is None:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    # obvious shop/collection links from the homepage nav
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base, a["href"])
        if urllib.parse.urlparse(href).netloc != urllib.parse.urlparse(base).netloc:
            continue
        if re.search(r"shop|store|product|collection|catalog|bouquet|flower|fleur|flores", href, re.I):
            if href not in seen and len(pages) < 4:
                seen.add(href)
                pages.append(href)
    items = []
    for i, page in enumerate(pages):
        page_soup = soup if i == 0 else None
        if page_soup is None:
            pr = fetch(page)
            if pr is None:
                continue
            page_soup = BeautifulSoup(pr.text, "html.parser")
        for script in page_soup.find_all("script", type="application/ld+json"):
            try:
                blob = json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            items.extend(_products_from_jsonld(blob, page))
        if len(items) >= MAX_ITEMS:
            break
    # dedupe by title
    out, titles = [], set()
    for it in items:
        if it["title"].lower() not in titles:
            titles.add(it["title"].lower())
            out.append(it)
    return out


def scrape_one(entry):
    base = entry.get("website") or ""
    host = urllib.parse.urlparse(base).netloc.lower()
    if not base or any(s in host for s in SKIP_HOSTS):
        return entry["iso2"], [], "skipped (no scrapable website)"
    if not robots_allows(base):
        return entry["iso2"], [], "skipped (robots.txt disallows)"
    for strategy in (try_shopify, try_woocommerce, try_jsonld):
        try:
            items = strategy(base)
        except Exception as exc:  # a broken site must never kill the run
            items = []
        if items:
            return entry["iso2"], items[:MAX_ITEMS], strategy.__name__
        time.sleep(0.3)
    return entry["iso2"], [], "no products found"


def main():
    florists = json.load(FLORISTS_FILE.open())
    florists = [f for f in florists if f.get("name")]
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    catalog, stats = {}, {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(scrape_one, f): f for f in florists}
        for fut in as_completed(futures):
            iso2, items, how = fut.result()
            stats[iso2] = {"items": len(items), "via": how}
            if items:
                catalog[iso2] = items
            print(f"{iso2}: {len(items):3d} items ({how})", flush=True)
    result = {
        "generated_at": started,
        "florists_total": len(florists),
        "florists_with_items": len(catalog),
        "items_total": sum(len(v) for v in catalog.values()),
        "items": catalog,
        "stats": stats,
    }
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False))
    tmp.replace(CATALOG_FILE)
    print(
        f"\ncatalog: {result['items_total']} items from "
        f"{result['florists_with_items']}/{result['florists_total']} florists → {CATALOG_FILE}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
