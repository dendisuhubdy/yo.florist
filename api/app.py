"""Flower Aggregator API — serves the partner-florist directory behind yo.florist."""
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

DATA_FILE = Path("/opt/yoflorist/florists.json")
DB_FILE = Path("/opt/yoflorist/data/orders.db")
CATALOG_FILE = Path("/opt/yoflorist/data/catalog.json")  # written daily by scraper.py

# Live Stripe key comes from /etc/yoflorist/stripe.env via the systemd unit.
# When unset, orders fall back to the pre-launch placeholder payment block.
STRIPE_SECRET_KEY = os.environ.get("FLOWER_STRIPE_SECRET_KEY", "")
# Google Maps/Places key (PLACES_GO_SDK). Autocomplete prefers Google and
# silently falls back to OSM Photon while the key's project has no billing.
GOOGLE_MAPS_KEY = os.environ.get("PLACES_GO_SDK", "")
STRIPE_API = "https://api.stripe.com/v1"
SITE_URL = "https://yo.florist"
# currencies Stripe treats as having no minor unit (charge amounts are whole)
ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}

app = FastAPI(
    title="Flower Aggregator API",
    version="0.1.0",
    description=(
        "Public read-only API behind [yo.florist](https://yo.florist) — "
        "a directory of one vetted partner florist per country. "
        "Look countries up by name or ISO code, or free-text search the way the "
        "landing-page form does."
    ),
    contact={"name": "Flower Aggregator", "url": "https://yo.florist"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],  # POST is needed for /v1/orders from the browser
    allow_headers=["*"],
)

_cache = {"mtime": None, "data": []}


def _load() -> list[dict]:
    mtime = DATA_FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        with DATA_FILE.open() as f:
            _cache["data"] = [e for e in json.load(f) if e.get("name")]
        _cache["mtime"] = mtime
    return _cache["data"]


_cat_cache = {"mtime": None, "data": {}}


def _catalog() -> dict:
    """items-by-iso2 from the daily scrape; empty until the first run lands."""
    if not CATALOG_FILE.exists():
        return {}
    mtime = CATALOG_FILE.stat().st_mtime
    if _cat_cache["mtime"] != mtime:
        with CATALOG_FILE.open() as f:
            _cat_cache["data"] = json.load(f)
        _cat_cache["mtime"] = mtime
    return _cat_cache["data"]


def _norm(s: str | None) -> str:
    return (
        unicodedata.normalize("NFKD", s or "")
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .strip()
    )


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("https://docs.yo.florist")


@app.get("/v1/health", tags=["meta"], summary="Service health")
def health():
    return {"status": "ok", "countries": len(_load())}


@app.get("/v1/countries", tags=["directory"], summary="List covered countries")
def countries():
    data = sorted(_load(), key=lambda e: e["country"])
    return {
        "count": len(data),
        "countries": [
            {"iso2": e["iso2"], "country": e["country"], "city": e.get("city")}
            for e in data
        ],
    }


@app.get("/v1/florists", tags=["directory"], summary="Get the partner florist for a country")
def florists(
    country: str = Query(..., description="ISO-3166 alpha-2 code (e.g. `ID`) or country name (e.g. `Indonesia`)"),
):
    q = _norm(country)
    for e in _load():
        if q == e["iso2"].lower() or q == _norm(e["country"]):
            return e
    raise HTTPException(status_code=404, detail=f"No partner florist found for {country!r}")


@app.get("/v1/search", tags=["search"], summary="Free-text search, as used by the landing-page form")
def search(
    q: str = Query(..., min_length=2, description="Free text — an address, city, or country (e.g. `Jakarta, Indonesia`)"),
    date: str | None = Query(None, description="Requested delivery date (echoed back, e.g. `2026-07-22`)"),
):
    nq = _norm(q)
    tokens = {t for t in nq.replace(",", " ").split() if t}
    matches, seen = [], set()
    for e in _load():
        hit = (
            _norm(e["country"]) in nq
            or e["iso2"].lower() in tokens
            or (_norm(e.get("city")) and _norm(e.get("city")) in nq)
        )
        if hit and e["iso2"] not in seen:
            seen.add(e["iso2"])
            matches.append({**e, "items": _items_for(e, limit=3)})
    return {"query": q, "date": date, "count": len(matches), "matches": matches}


def _items_for(florist: dict, limit: int | None = None) -> list[dict]:
    """Catalog items for a florist, each carrying an honest source attribution."""
    items = (_catalog().get("items") or {}).get(florist["iso2"], [])
    if limit is not None:
        items = items[:limit]
    source = {
        "florist": florist["name"],
        "website": florist["website"],
        "city": florist["city"],
        "country": florist["country"],
    }
    return [{**it, "source": source} for it in items]


@app.get("/v1/catalog", tags=["catalog"], summary="Scraped bouquet catalog — one country's, or browse everything")
def catalog(
    country: str | None = Query(None, description="ISO-3166 alpha-2 code or country name; omit to browse the full catalog"),
    limit: int = Query(48, ge=1, le=200, description="Browse mode: page size"),
    offset: int = Query(0, ge=0, description="Browse mode: page start"),
):
    cat = _catalog()
    if country:
        florist = _find_florist(country)
        items = _items_for(florist)
        return {
            "iso2": florist["iso2"],
            "country": florist["country"],
            "florist": florist["name"],
            "florist_website": florist["website"],
            "generated_at": cat.get("generated_at"),
            "count": len(items),
            "items": items,
        }
    by_iso = {e["iso2"]: e for e in _load()}
    everything = []
    for iso2 in sorted(cat.get("items") or {}, key=lambda k: by_iso.get(k, {}).get("country", "")):
        florist = by_iso.get(iso2)
        if florist:
            everything.extend({**it, "iso2": iso2} for it in _items_for(florist))
    return {
        "generated_at": cat.get("generated_at"),
        "total": len(everything),
        "offset": offset,
        "limit": limit,
        "items": everything[offset : offset + limit],
    }


# ─── address autocomplete (Google Places when billed, OSM Photon fallback) ───

@app.get("/v1/geo/autocomplete", tags=["geo"], summary="Address autocomplete for the checkout page")
def geo_autocomplete(
    q: str = Query(..., min_length=3, max_length=200, description="Partial address"),
    country: str | None = Query(None, max_length=2, description="ISO-2 country to restrict results to"),
):
    cc = (country or "").lower()
    if GOOGLE_MAPS_KEY:
        try:
            params = {"input": q, "key": GOOGLE_MAPS_KEY}
            if cc:
                params["components"] = f"country:{cc}"
            r = requests.get(
                "https://maps.googleapis.com/maps/api/place/autocomplete/json",
                params=params, timeout=8,
            )
            js = r.json()
            if js.get("status") == "OK":
                return {
                    "provider": "google",
                    "suggestions": [
                        {"label": p["description"], "place_id": p["place_id"], "fields": None}
                        for p in js.get("predictions", [])[:6]
                    ],
                }
        except requests.RequestException:
            pass
    try:
        r = requests.get(
            "https://photon.komoot.io/api/",
            params={"q": q, "limit": 10, "lang": "en"},
            headers={"User-Agent": "yo.florist checkout (contact: dendi.suhubdy@gmail.com)"},
            timeout=8,
        )
        suggestions = []
        for f in r.json().get("features", []):
            p = f.get("properties", {})
            if cc and (p.get("countrycode") or "").lower() != cc:
                continue
            parts = [p.get("name"), p.get("street"), p.get("housenumber"), p.get("district"),
                     p.get("city"), p.get("state"), p.get("postcode"), p.get("country")]
            label = ", ".join(dict.fromkeys(str(x) for x in parts if x))
            street = " ".join(str(x) for x in (p.get("street"), p.get("housenumber")) if x) or p.get("name")
            lng, lat = ((f.get("geometry") or {}).get("coordinates") or [None, None])[:2]
            suggestions.append({
                "label": label,
                "place_id": None,
                "fields": {
                    "street": street,
                    "city": p.get("city") or p.get("district") or p.get("county"),
                    "postal_code": p.get("postcode"),
                    "lat": lat, "lng": lng,
                },
            })
            if len(suggestions) >= 6:
                break
        return {"provider": "photon", "suggestions": suggestions}
    except requests.RequestException:
        return {"provider": "none", "suggestions": []}


@app.get("/v1/geo/details", tags=["geo"], summary="Resolve a Google place_id to address fields")
def geo_details(place_id: str = Query(..., max_length=300)):
    if not GOOGLE_MAPS_KEY:
        raise HTTPException(status_code=404, detail="Place details unavailable")
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "place_id": place_id,
                "fields": "formatted_address,address_component,geometry/location",
                "key": GOOGLE_MAPS_KEY,
            },
            timeout=8,
        )
        js = r.json()
        if js.get("status") != "OK":
            raise HTTPException(status_code=502, detail="Place lookup failed")
        res = js["result"]
        comp = {t: c["long_name"] for c in res.get("address_components", []) for t in c.get("types", [])}
        loc = (res.get("geometry") or {}).get("location") or {}
        street = " ".join(x for x in (comp.get("route"), comp.get("street_number")) if x)
        return {
            "label": res.get("formatted_address"),
            "fields": {
                "street": street or comp.get("sublocality") or "",
                "city": comp.get("locality") or comp.get("administrative_area_level_2") or "",
                "postal_code": comp.get("postal_code"),
                "lat": loc.get("lat"), "lng": loc.get("lng"),
            },
        }
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Place lookup failed")


# ─── orders ───

def _db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            iso2 TEXT NOT NULL,
            florist TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            email TEXT NOT NULL,
            address TEXT NOT NULL,
            delivery_date TEXT,
            budget_usd INTEGER,
            message TEXT
        )"""
    )
    # migrate older databases in place
    have = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    for col, typ in (
        ("item_title", "TEXT"),
        ("item_price", "REAL"),
        ("item_currency", "TEXT"),
        ("item_url", "TEXT"),
        ("stripe_session_id", "TEXT"),
        ("checkout_url", "TEXT"),
        ("recipient_name", "TEXT"),
        ("recipient_phone", "TEXT"),
        ("street", "TEXT"),
        ("city", "TEXT"),
        ("postal_code", "TEXT"),
        ("delivery_instructions", "TEXT"),
        ("lat", "REAL"),
        ("lng", "REAL"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
    return conn


def _find_florist(country: str) -> dict:
    q = _norm(country)
    for e in _load():
        if q == e["iso2"].lower() or q == _norm(e["country"]):
            return e
    raise HTTPException(status_code=404, detail=f"No partner florist found for {country!r}")


def _placeholder_payment() -> dict:
    return {
        "provider": "stripe",
        "mode": "placeholder",
        "checkout_url": None,
        "note": (
            "Payments are not yet enabled — no charge has been made. "
            "A secure payment link will be emailed when payments go live."
        ),
    }


def _create_checkout(oid: str, order: "OrderIn", florist: dict) -> tuple[str, str] | None:
    """Create a Stripe hosted-Checkout session; returns (session_id, url) or None."""
    if order.item_title and order.item_price:
        amount = order.item_price
        currency = (order.item_currency or "usd").lower()
        name = order.item_title
        desc = f"Made & delivered by {florist['name']}, {florist['city']} — sourced from their own catalog"
    else:
        amount = float(order.budget_usd or 65)
        currency = "usd"
        name = f"Bouquet by {florist['name']} ({florist['city']}, {florist['country']})"
        desc = "Florist's choice bouquet, delivery included"
    unit_amount = int(round(amount)) if currency in ZERO_DECIMAL else int(round(amount * 100))
    payload = {
        "mode": "payment",
        # explicit: the account has no per-currency dashboard config yet, and
        # without this Stripe rejects sessions ("No valid payment method types")
        "payment_method_types[0]": "card",
        "customer_email": order.email,
        "client_reference_id": oid,
        "metadata[order_id]": oid,
        "metadata[florist]": florist["name"],
        "metadata[iso2]": florist["iso2"],
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency,
        "line_items[0][price_data][unit_amount]": str(unit_amount),
        "line_items[0][price_data][product_data][name]": name[:250],
        "line_items[0][price_data][product_data][description]": desc[:250],
        "success_url": f"{SITE_URL}/thanks.html?order={oid}",
        "cancel_url": f"{SITE_URL}/?payment=cancelled",
    }
    try:
        r = requests.post(
            f"{STRIPE_API}/checkout/sessions",
            auth=(STRIPE_SECRET_KEY, ""),
            data=payload,
            timeout=20,
        )
        if not r.ok:
            print(f"stripe checkout failed for {oid}: {r.status_code} {r.text[:300]}", flush=True)
            return None
        js = r.json()
        return js["id"], js["url"]
    except requests.RequestException as exc:
        print(f"stripe checkout error for {oid}: {exc}", flush=True)
        return None


def _refresh_payment_status(row: dict) -> str:
    """Poll Stripe for a pending order's session; mark paid when it is."""
    status = row["status"]
    if status != "pending_payment" or not STRIPE_SECRET_KEY or not row.get("stripe_session_id"):
        return status
    try:
        r = requests.get(
            f"{STRIPE_API}/checkout/sessions/{row['stripe_session_id']}",
            auth=(STRIPE_SECRET_KEY, ""),
            timeout=15,
        )
        if r.ok and r.json().get("payment_status") == "paid":
            with _db() as conn:
                conn.execute("UPDATE orders SET status='paid' WHERE id=?", (row["id"],))
            return "paid"
    except requests.RequestException:
        pass
    return status


class OrderIn(BaseModel):
    country: str = Field(..., description="ISO-3166 alpha-2 code or country name — the order routes to this country's partner florist")
    customer_name: str = Field(..., min_length=2, max_length=120)
    # NB: validated explicitly in create_order — Field(pattern=...) is silently
    # ignored by pydantic v1, which Ubuntu's apt-packaged FastAPI runs on.
    email: str = Field(...)
    address: str = Field(..., min_length=5, max_length=500, description="Recipient delivery address")
    delivery_date: str | None = Field(None, description="Requested delivery date, e.g. 2026-07-28")
    budget_usd: int | None = Field(None, ge=10, le=10000, description="Bouquet budget in USD when ordering without a catalog item")
    message: str | None = Field(None, max_length=500, description="Card message for the recipient")
    # catalog-item orders: which scraped product the customer chose
    item_title: str | None = Field(None, max_length=300)
    item_price: float | None = Field(None, ge=0)
    item_currency: str | None = Field(None, max_length=8)
    item_url: str | None = Field(None, max_length=1000, description="Product page on the source florist's own site")
    # structured recipient/delivery details from the checkout page
    recipient_name: str | None = Field(None, max_length=120)
    recipient_phone: str | None = Field(None, max_length=40)
    street: str | None = Field(None, max_length=300)
    city: str | None = Field(None, max_length=120)
    postal_code: str | None = Field(None, max_length=20)
    delivery_instructions: str | None = Field(None, max_length=500)
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)


@app.post("/v1/orders", status_code=201, tags=["orders"], summary="Create a purchase order (payment activates when Stripe is approved)")
def create_order(order: OrderIn):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", order.email):
        raise HTTPException(status_code=422, detail="Invalid email address")
    florist = _find_florist(order.country)
    oid = "YF-" + uuid.uuid4().hex[:8].upper()
    session_id, checkout_url = None, None
    if STRIPE_SECRET_KEY:
        created = _create_checkout(oid, order, florist)
        if created:
            session_id, checkout_url = created
    with _db() as conn:
        conn.execute(
            "INSERT INTO orders (id, created_at, status, iso2, florist, customer_name,"
            " email, address, delivery_date, budget_usd, message,"
            " item_title, item_price, item_currency, item_url,"
            " stripe_session_id, checkout_url,"
            " recipient_name, recipient_phone, street, city, postal_code,"
            " delivery_instructions, lat, lng)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                oid,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "pending_payment",
                florist["iso2"],
                florist["name"],
                order.customer_name,
                order.email,
                order.address,
                order.delivery_date,
                order.budget_usd,
                order.message,
                order.item_title,
                order.item_price,
                order.item_currency,
                order.item_url,
                session_id,
                checkout_url,
                order.recipient_name,
                order.recipient_phone,
                order.street,
                order.city,
                order.postal_code,
                order.delivery_instructions,
                order.lat,
                order.lng,
            ),
        )
    item = None
    if order.item_title:
        item = {
            "title": order.item_title,
            "price": order.item_price,
            "currency": order.item_currency,
            "url": order.item_url,
            "fulfilled_by": florist["name"],
        }
    if not STRIPE_SECRET_KEY:
        payment = _placeholder_payment()
    elif checkout_url:
        payment = {
            "provider": "stripe",
            "mode": "live",
            "checkout_url": checkout_url,
            "note": "Complete payment on Stripe's secure page — the florist is engaged once payment clears.",
        }
    else:
        payment = {
            "provider": "stripe",
            "mode": "error",
            "checkout_url": None,
            "note": "The payment session couldn't be created — the order is saved and we'll email you a payment link.",
        }
    return {
        "order_id": oid,
        "status": "pending_payment",
        "routed_to": {
            "iso2": florist["iso2"],
            "country": florist["country"],
            "florist": florist["name"],
            "city": florist["city"],
            "website": florist["website"],
        },
        "item": item,
        "payment": payment,
    }


@app.get("/v1/orders/{order_id}", tags=["orders"], summary="Get an order's status")
def get_order(order_id: str):
    with _db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown order {order_id!r}")
    o = dict(row)
    o["status"] = _refresh_payment_status(o)
    o["payment"] = {
        "provider": "stripe",
        "mode": "live" if STRIPE_SECRET_KEY else "placeholder",
        # only hand the checkout link back while payment is still owed
        "checkout_url": o["checkout_url"] if o["status"] == "pending_payment" else None,
    }
    # the customer-facing status endpoint doesn't echo back PII or session ids
    for k in ("email", "address", "customer_name", "message", "stripe_session_id", "checkout_url",
              "recipient_name", "recipient_phone", "street", "city", "postal_code",
              "delivery_instructions", "lat", "lng"):
        o.pop(k, None)
    return o
