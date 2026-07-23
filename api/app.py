"""Flower Aggregator API — serves the partner-florist directory behind yo.florist."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

DATA_FILE = Path("/opt/yoflorist/florists.json")
DB_FILE = Path("/opt/yoflorist/data/orders.db")
CATALOG_FILE = Path("/opt/yoflorist/data/catalog.json")  # written daily by scraper.py
MEDIA_DIR = Path("/opt/yoflorist/media")  # partner-uploaded photos, served at yo.florist/media/
MEDIA_URL = "https://yo.florist/media"
MAX_UPLOAD = 5 * 1024 * 1024
MAX_ITEMS_PER_PARTNER = 100

# Live Stripe key comes from /etc/yoflorist/stripe.env via the systemd unit.
# When unset, orders fall back to the pre-launch placeholder payment block.
STRIPE_SECRET_KEY = os.environ.get("FLOWER_STRIPE_SECRET_KEY", "")
# Google Maps/Places key (PLACES_GO_SDK). Autocomplete prefers Google and
# silently falls back to OSM Photon while the key's project has no billing.
GOOGLE_MAPS_KEY = os.environ.get("PLACES_GO_SDK", "")
# Admin key for the moderation dashboard at yo.florist/admin (env file only)
ADMIN_KEY = os.environ.get("YF_ADMIN_KEY", "")
# Resend (transactional email): partner welcome/approval + paid-order handoff.
# When unset, emails are skipped and orders stay visible in /admin only.
RESEND_API_KEY = os.environ.get("YF_RESEND_API_KEY", "")
EMAIL_FROM = "yo.florist <orders@yo.florist>"
ADMIN_EMAIL = os.environ.get("YF_ADMIN_EMAIL", "dendi.suhubdy@gmail.com")
STRIPE_API = "https://api.stripe.com/v1"
SITE_URL = "https://yo.florist"
# IP → country (DB-IP Lite, monthly file, "IP Geolocation by DB-IP" attribution)
GEOIP_FILE = Path("/opt/yoflorist/data/dbip-country-lite.mmdb")
# daily USD-based FX rates (open.er-api.com, keyless), cached in memory + on disk
FX_FILE = Path("/opt/yoflorist/data/fx.json")
FX_TTL = 12 * 3600

try:
    import maxminddb
except ImportError:  # dev machines without python3-maxminddb: locale falls back
    maxminddb = None
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
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  # browser needs POST for orders, PUT/DELETE for the partner dashboard
    allow_headers=["*"],
)

_cache = {"mtime": None, "data": []}


def _load() -> list[dict]:
    mtime = DATA_FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        with DATA_FILE.open() as f:
            raw = json.load(f)
        _cache["raw"] = raw  # every country, even ones without a scraped-directory florist
        _cache["data"] = [e for e in raw if e.get("name")]
        _cache["mtime"] = mtime
    return _cache["data"]


def _resolve_country(country: str) -> dict:
    """Match any real country (partner florists can cover directory gaps)."""
    _load()
    q = _norm(country)
    for e in _cache["raw"]:
        if q == e["iso2"].lower() or q == _norm(e["country"]):
            return e
    raise HTTPException(status_code=404, detail=f"Unknown country {country!r}")


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
    _load()
    out = [
        {"iso2": e["iso2"], "country": e["country"], "city": e.get("city")}
        for e in _cache["data"]
    ]
    # partner shops can cover countries the scraped directory doesn't
    covered = {e["iso2"] for e in out}
    by_iso = {e["iso2"]: e for e in _cache["raw"]}
    with _db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.iso2, p.city FROM partners p JOIN partner_items pi ON pi.partner_id = p.id"
            " WHERE p.status = 'active' AND pi.status = 'active'").fetchall()
    for iso2, city in rows:
        if iso2 not in covered and iso2 in by_iso:
            covered.add(iso2)
            out.append({"iso2": iso2, "country": by_iso[iso2]["country"], "city": city})
    out.sort(key=lambda e: e["country"])
    return {"count": len(out), "countries": out}


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
    # searchable = directory florists + gap countries covered only by partner shops
    partner_isos = _partner_isos()
    candidates = list(_load()) + [
        e for e in _cache["raw"] if not e.get("name") and e["iso2"] in partner_isos
    ]
    matches, seen = [], set()
    for e in candidates:
        hit = (
            _norm(e["country"]) in nq
            or e["iso2"].lower() in tokens
            or (_norm(e.get("city")) and _norm(e.get("city")) in nq)
        )
        if hit and e["iso2"] not in seen:
            seen.add(e["iso2"])
            m = {**e, "items": _items_for(e, limit=3)}
            if not m.get("name") and m["items"]:
                src = m["items"][0]["source"]
                m.update(name=src["florist"], city=src["city"], website=src["website"])
            matches.append(m)
    return {"query": q, "date": date, "count": len(matches), "matches": matches}


def _items_for(florist: dict, limit: int | None = None) -> list[dict]:
    """Catalog items for a country: partner-uploaded first, then scraped —
    each carrying an honest source attribution."""
    items = list(_partner_catalog_items(florist["iso2"]))
    if florist.get("name"):
        source = {
            "florist": florist["name"],
            "website": florist["website"],
            "city": florist["city"],
            "country": florist["country"],
        }
        items += [{**it, "source": source} for it in (_catalog().get("items") or {}).get(florist["iso2"], [])]
    return items if limit is None else items[:limit]


@app.get("/v1/catalog", tags=["catalog"], summary="Scraped bouquet catalog — one country's, or browse everything")
def catalog(
    country: str | None = Query(None, description="ISO-3166 alpha-2 code or country name; omit to browse the full catalog"),
    limit: int = Query(48, ge=1, le=200, description="Browse mode: page size"),
    offset: int = Query(0, ge=0, description="Browse mode: page start"),
):
    cat = _catalog()
    if country:
        centry = _resolve_country(country)  # partner shops can cover directory gaps
        items = _items_for(centry)
        florist_name = centry.get("name") or (items[0]["source"]["florist"] if items else None)
        return {
            "iso2": centry["iso2"],
            "country": centry["country"],
            "florist": florist_name,
            "florist_website": centry.get("website") or (items[0]["source"]["website"] if items else None),
            "generated_at": cat.get("generated_at"),
            "count": len(items),
            "items": items,
        }
    _load()
    by_iso = {e["iso2"]: e for e in _cache["raw"]}
    covered = set(cat.get("items") or {}) | _partner_isos()
    everything = []
    for iso2 in sorted(covered, key=lambda k: by_iso.get(k, {}).get("country", "")):
        centry = by_iso.get(iso2)
        if centry:
            everything.extend({**it, "iso2": iso2} for it in _items_for(centry))
    return {
        "generated_at": cat.get("generated_at"),
        "total": len(everything),
        "offset": offset,
        "limit": limit,
        "items": everything[offset : offset + limit],
    }


# ─── visitor locale: IP → country → display currency + FX rates ───

# grouped currency zones first, then one-offs; territories map to their parent currency
_CURRENCY_ZONES = {
    "EUR": "AD AT AX BE BL CY DE EE ES FI FR GF GP GR HR IE IT LT LU LV MC ME MF MQ MT NL PM PT RE SI SK SM VA XK YT",
    "USD": "AS EC FM GU IO MH MP PA PR PW SV TC TL UM US VG VI ZW",
    "XOF": "BJ BF CI GW ML NE SN TG",
    "XAF": "CM CF CG GA GQ TD",
    "XCD": "AG AI DM GD KN LC MS VC",
    "XPF": "NC PF WF",
    "GBP": "GB GG GS IM JE",
    "AUD": "AU CC CX KI NF NR TV",
    "NZD": "CK NU NZ PN TK",
    "DKK": "DK FO GL",
    "NOK": "BV NO SJ",
    "CHF": "CH LI",
    "MAD": "EH MA",
    "ILS": "IL PS",
    "ZAR": "ZA",
}
COUNTRY_CURRENCY = {iso: cur for cur, isos in _CURRENCY_ZONES.items() for iso in isos.split()}
COUNTRY_CURRENCY.update({
    "AF": "AFN", "AL": "ALL", "DZ": "DZD", "AO": "AOA", "AR": "ARS", "AM": "AMD", "AW": "AWG",
    "AZ": "AZN", "BS": "BSD", "BH": "BHD", "BD": "BDT", "BB": "BBD", "BY": "BYN", "BZ": "BZD",
    "BM": "BMD", "BT": "BTN", "BO": "BOB", "BA": "BAM", "BW": "BWP", "BR": "BRL", "BN": "BND",
    "BG": "BGN", "BI": "BIF", "KH": "KHR", "CA": "CAD", "CV": "CVE", "KY": "KYD", "CL": "CLP",
    "CN": "CNY", "CO": "COP", "KM": "KMF", "CD": "CDF", "CR": "CRC", "CU": "CUP", "CW": "ANG",
    "CZ": "CZK", "DJ": "DJF", "DO": "DOP", "EG": "EGP", "ER": "ERN", "SZ": "SZL", "ET": "ETB",
    "FJ": "FJD", "FK": "FKP", "GM": "GMD", "GE": "GEL", "GH": "GHS", "GI": "GIP", "GT": "GTQ",
    "GN": "GNF", "GY": "GYD", "HT": "HTG", "HN": "HNL", "HK": "HKD", "HU": "HUF", "IS": "ISK",
    "IN": "INR", "ID": "IDR", "IR": "IRR", "IQ": "IQD", "JM": "JMD", "JP": "JPY", "JO": "JOD",
    "KZ": "KZT", "KE": "KES", "KW": "KWD", "KG": "KGS", "LA": "LAK", "LB": "LBP", "LR": "LRD",
    "LS": "LSL", "LY": "LYD", "MO": "MOP", "MG": "MGA", "MW": "MWK", "MY": "MYR", "MV": "MVR",
    "MR": "MRU", "MU": "MUR", "MX": "MXN", "MD": "MDL", "MN": "MNT", "MZ": "MZN", "MM": "MMK",
    "NA": "NAD", "NP": "NPR", "NI": "NIO", "NG": "NGN", "KP": "KPW", "MK": "MKD", "OM": "OMR",
    "PK": "PKR", "PG": "PGK", "PY": "PYG", "PE": "PEN", "PH": "PHP", "PL": "PLN", "QA": "QAR",
    "RO": "RON", "RU": "RUB", "RW": "RWF", "WS": "WST", "SA": "SAR", "RS": "RSD", "SC": "SCR",
    "SH": "SHP", "SL": "SLE", "SG": "SGD", "SB": "SBD", "SO": "SOS", "KR": "KRW", "SS": "SSP",
    "LK": "LKR", "SD": "SDG", "SR": "SRD", "SE": "SEK", "SY": "SYP", "TW": "TWD", "TJ": "TJS",
    "TZ": "TZS", "TH": "THB", "TO": "TOP", "TT": "TTD", "TN": "TND", "TR": "TRY", "TM": "TMT",
    "UG": "UGX", "UA": "UAH", "AE": "AED", "UY": "UYU", "UZ": "UZS", "VU": "VUV", "VE": "VES",
    "VN": "VND", "YE": "YER", "ZM": "ZMW",
})

_geoip = {"reader": None, "tried": False}
_fx_cache = {"fetched": 0.0, "rates": None}


def _ip_country(request: Request) -> str | None:
    """Country ISO2 for the calling IP — X-Real-IP is set by nginx from the socket."""
    if maxminddb is None or not GEOIP_FILE.exists():
        return None
    if _geoip["reader"] is None and not _geoip["tried"]:
        _geoip["tried"] = True
        try:
            _geoip["reader"] = maxminddb.open_database(str(GEOIP_FILE))
        except Exception as exc:
            print(f"geoip open failed: {exc}", flush=True)
    if _geoip["reader"] is None:
        return None
    ip = (request.headers.get("x-real-ip")
          or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    try:
        hit = _geoip["reader"].get(ip)
        return hit["country"]["iso_code"] if hit else None
    except (ValueError, KeyError, TypeError):
        return None


def _fx_rates() -> dict | None:
    """USD-based rates, refreshed every 12h; last good copy persists across restarts."""
    now = time.time()
    if _fx_cache["rates"] and now - _fx_cache["fetched"] < FX_TTL:
        return _fx_cache["rates"]
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        js = r.json()
        if js.get("result") == "success" and js.get("rates"):
            _fx_cache.update(fetched=now, rates=js["rates"])
            FX_FILE.parent.mkdir(parents=True, exist_ok=True)
            FX_FILE.write_text(json.dumps({"fetched": now, "rates": js["rates"]}))
            return js["rates"]
    except (requests.RequestException, ValueError) as exc:
        print(f"fx fetch failed: {exc}", flush=True)
    if _fx_cache["rates"]:  # stale beats none
        return _fx_cache["rates"]
    try:
        disk = json.loads(FX_FILE.read_text())
        _fx_cache.update(fetched=disk["fetched"], rates=disk["rates"])
        return disk["rates"]
    except (OSError, ValueError, KeyError):
        return None


@app.get("/v1/locale", tags=["geo"], summary="Visitor's country + display currency, with FX rates for client-side conversion")
def locale(request: Request, country: str | None = Query(None, max_length=2, description="Override the IP-derived ISO-2 country (e.g. for testing)")):
    iso2 = (country or "").upper() or _ip_country(request)
    currency = COUNTRY_CURRENCY.get(iso2 or "")
    rates = _fx_rates()
    if currency and rates and currency not in rates:
        currency = None
    return {
        "country": iso2,
        "currency": currency,  # null → show shop prices as-is
        "base": "USD",
        "rates": rates,
        "note": "Display conversion only — orders are charged in the florist's own currency.",
        "attribution": "IP Geolocation by DB-IP (db-ip.com)",
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


# ─── partner florists: self-serve catalog (the "sell on yo.florist" flow) ───

def _partner_auth(x_partner_key: str = Header(..., description="Secret key issued at signup")) -> dict:
    key_hash = hashlib.sha256(x_partner_key.encode()).hexdigest()
    with _db() as conn:
        row = conn.execute("SELECT * FROM partners WHERE key_hash = ?", (key_hash,)).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid partner key")
    if row["status"] == "rejected":
        raise HTTPException(status_code=403, detail="This shop application was not approved. Contact us if you think that's a mistake.")
    if row["status"] == "suspended":
        raise HTTPException(status_code=403, detail="This shop is currently suspended. Contact us to resolve it.")
    # 'pending' partners may manage their catalog; it goes public on approval
    return dict(row)


def _partner_public(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "shop_name", "iso2", "country", "city", "website", "instagram", "status", "created_at")}


class PartnerIn(BaseModel):
    shop_name: str = Field(..., min_length=2, max_length=120)
    country: str = Field(..., description="ISO-3166 alpha-2 code or country name")
    city: str = Field(..., min_length=2, max_length=120)
    email: str = Field(...)
    phone: str | None = Field(None, max_length=40)
    website: str | None = Field(None, max_length=500)
    instagram: str | None = Field(None, max_length=200, description="Handle or profile URL")
    description: str | None = Field(None, max_length=1000)


class PartnerItemIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=300)
    # cap must fit high-denomination currencies: a normal IDR bouquet is ~1,000,000
    price: float = Field(..., gt=0, le=100_000_000)
    currency: str = Field(..., min_length=3, max_length=3, description="ISO code, e.g. USD, EUR, IDR")
    image_url: str | None = Field(None, max_length=1000, description="Photo URL — or use /v1/partners/upload")
    product_url: str | None = Field(None, max_length=1000, description="This item on your own site/Instagram, if any")
    description: str | None = Field(None, max_length=500)


@app.post("/v1/partners", status_code=201, tags=["partners"], summary="Register as a partner florist")
def register_partner(p: PartnerIn):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", p.email):
        raise HTTPException(status_code=422, detail="Invalid email address")
    centry = _resolve_country(p.country)
    ig = (p.instagram or "").strip().lstrip("@")
    if ig and not ig.startswith("http"):
        ig = f"https://instagram.com/{ig}"
    key = "yfk_" + secrets.token_hex(20)
    pid = "pf_" + uuid.uuid4().hex[:10]
    with _db() as conn:
        conn.execute(
            "INSERT INTO partners (id, created_at, status, key_hash, shop_name, iso2, country,"
            " city, email, phone, website, instagram, description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, datetime.now(timezone.utc).isoformat(timespec="seconds"), "pending",
             hashlib.sha256(key.encode()).hexdigest(), p.shop_name.strip(), centry["iso2"],
             centry["country"], p.city.strip(), p.email.strip(), p.phone, p.website, ig or None, p.description),
        )
    _notify_partner_welcome(p.email.strip(), p.shop_name.strip())
    return {
        "partner": _partner_public({"id": pid, "shop_name": p.shop_name.strip(), "iso2": centry["iso2"],
                                    "country": centry["country"], "city": p.city.strip(), "website": p.website,
                                    "instagram": ig or None, "status": "pending", "created_at": None}),
        "partner_key": key,
        "note": ("Store this key safely — it is shown only once and is how you manage your catalog at "
                 "https://yo.florist/dashboard. Your shop is under review; you can add your catalog now "
                 "and it goes live the moment you're approved."),
    }


@app.get("/v1/partners/me", tags=["partners"], summary="Your partner profile")
def partner_me(partner: dict = Depends(_partner_auth)):
    with _db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM partner_items WHERE partner_id = ? AND status = 'active'",
                         (partner["id"],)).fetchone()[0]
    return {**_partner_public(partner), "items": n}


@app.get("/v1/partners/items", tags=["partners"], summary="List your catalog items")
def partner_items_list(partner: dict = Depends(_partner_auth)):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, title, price, currency, image, url, description FROM partner_items"
            " WHERE partner_id = ? AND status = 'active' ORDER BY created_at DESC, id DESC",
            (partner["id"],)).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


@app.post("/v1/partners/items", status_code=201, tags=["partners"], summary="Add a catalog item")
def partner_item_add(item: PartnerItemIn, partner: dict = Depends(_partner_auth)):
    with _db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM partner_items WHERE partner_id = ? AND status = 'active'",
                         (partner["id"],)).fetchone()[0]
        if n >= MAX_ITEMS_PER_PARTNER:
            raise HTTPException(status_code=409, detail=f"Catalog limit of {MAX_ITEMS_PER_PARTNER} items reached")
        cur = conn.execute(
            "INSERT INTO partner_items (partner_id, created_at, status, title, price, currency,"
            " image, url, description) VALUES (?,?,?,?,?,?,?,?,?)",
            (partner["id"], datetime.now(timezone.utc).isoformat(timespec="seconds"), "active",
             item.title.strip(), round(item.price, 2), item.currency.upper(),
             item.image_url, item.product_url, item.description),
        )
    return {"id": cur.lastrowid, "status": "active"}


@app.put("/v1/partners/items/{item_id}", tags=["partners"], summary="Update a catalog item")
def partner_item_update(item_id: int, item: PartnerItemIn, partner: dict = Depends(_partner_auth)):
    with _db() as conn:
        cur = conn.execute(
            "UPDATE partner_items SET title=?, price=?, currency=?, image=?, url=?, description=?"
            " WHERE id=? AND partner_id=? AND status='active'",
            (item.title.strip(), round(item.price, 2), item.currency.upper(), item.image_url,
             item.product_url, item.description, item_id, partner["id"]),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such item")
    return {"id": item_id, "status": "updated"}


@app.delete("/v1/partners/items/{item_id}", tags=["partners"], summary="Remove a catalog item")
def partner_item_delete(item_id: int, partner: dict = Depends(_partner_auth)):
    with _db() as conn:
        cur = conn.execute("UPDATE partner_items SET status='deleted' WHERE id=? AND partner_id=? AND status='active'",
                           (item_id, partner["id"]))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such item")
    return {"id": item_id, "status": "deleted"}


@app.get("/v1/partners/orders", tags=["partners"], summary="Paid orders routed to your shop")
def partner_orders(partner: dict = Depends(_partner_auth)):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, status, item_title, item_price, item_currency, budget_usd,"
            " recipient_name, recipient_phone, street, city, postal_code, address,"
            " delivery_instructions, delivery_date, delivery_time, confirm_time_with_customer,"
            " message, customer_name"
            " FROM orders WHERE partner_id = ? AND status = 'paid' ORDER BY created_at DESC",
            (partner["id"],)).fetchall()
    return {"count": len(rows), "orders": [dict(r) for r in rows]}


@app.post("/v1/partners/upload", tags=["partners"], summary="Upload a bouquet photo (jpeg/png/webp, ≤5MB)")
async def partner_upload(partner: dict = Depends(_partner_auth), file: UploadFile = File(...)):
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(file.content_type)
    if ext is None:
        raise HTTPException(status_code=415, detail="Use a JPEG, PNG or WebP image")
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Image too large (max 5MB)")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{partner['id']}-{uuid.uuid4().hex[:10]}{ext}"
    (MEDIA_DIR / name).write_bytes(data)
    return {"url": f"{MEDIA_URL}/{name}"}


def _partner_catalog_items(iso2: str) -> list[dict]:
    """Live partner items for a country, shaped like scraped catalog items."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT pi.id, pi.partner_id, pi.title, pi.price, pi.currency, pi.image, pi.url,"
            " p.shop_name, p.city AS pcity, p.country AS pcountry, p.website AS pweb, p.instagram"
            " FROM partner_items pi JOIN partners p ON p.id = pi.partner_id"
            " WHERE p.iso2 = ? AND pi.status = 'active' AND p.status = 'active'"
            " ORDER BY pi.created_at DESC, pi.id DESC",
            (iso2,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        link = d["pweb"] or d["instagram"] or ""
        out.append({
            "title": d["title"], "price": d["price"], "currency": d["currency"],
            "image": d["image"], "url": d["url"] or link, "partner_id": d["partner_id"],
            "source": {"florist": d["shop_name"], "website": link, "city": d["pcity"],
                       "country": d["pcountry"], "partner": True},
        })
    return out


# ─── transactional email (Resend) ───

def _send_email(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY or not to:
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html},
            timeout=10,
        )
        if not r.ok:
            print(f"resend failed to {to}: {r.status_code} {r.text[:200]}", flush=True)
        return r.ok
    except requests.RequestException as exc:
        print(f"resend error to {to}: {exc}", flush=True)
        return False


def _email_shell(body: str) -> str:
    return (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;padding:24px;color:#22301f">'
        '<p style="font-size:1.3em;margin:0 0 18px">🌸 <strong>yo.florist</strong></p>'
        f"{body}"
        '<p style="margin-top:28px;font-size:0.85em;color:#5c7a54">yo.florist — every florist in your city, one search.<br>'
        'Questions? Just reply to this email.</p></div>'
    )


def _notify_partner_welcome(email: str, shop_name: str):
    _send_email(email, f"{shop_name} is registered on yo.florist — under review", _email_shell(
        f"<p>Welcome aboard! <strong>{shop_name}</strong> is registered and under review "
        "(usually done the same day).</p>"
        "<p>You can build your catalog right now at "
        '<a href="https://yo.florist/dashboard">yo.florist/dashboard</a> with the partner key '
        "shown at signup — everything goes live the moment you're approved. "
        "The key was shown only once, so keep it safe; we can't recover it, only issue a new one.</p>"
        "<p>You'll get another email from us when you're live, and one for every paid order "
        "with the recipient's full delivery details.</p>"
    ))


def _notify_partner_approved(email: str, shop_name: str, iso2: str):
    _send_email(email, f"{shop_name} is now live on yo.florist 🌸", _email_shell(
        f"<p><strong>{shop_name}</strong> is approved — your bouquets are live at "
        f'<a href="https://yo.florist/flowers?country={iso2}">yo.florist/flowers</a> '
        "with your shop named on every card.</p>"
        "<p>Paid orders arrive by email with the recipient's address, phone, delivery date "
        "and card message — you make and deliver. Manage your catalog anytime at "
        '<a href="https://yo.florist/dashboard">yo.florist/dashboard</a>.</p>'
    ))


def _order_details_html(o: dict) -> str:
    when = o.get("delivery_date") or "date not set"
    if o.get("confirm_time_with_customer"):
        when += " — ☎️ please call the recipient to agree a convenient time before delivering"
    elif o.get("delivery_time"):
        when += f", {o['delivery_time']}"
    if o.get("item_title"):
        what = f"{o['item_title']} — {o.get('item_currency') or ''} {o.get('item_price') or ''}".strip()
    else:
        what = f"Florist's choice bouquet — ${o.get('budget_usd') or 65} USD budget"
    addr = ", ".join(str(x) for x in (o.get("street"), o.get("city"), o.get("postal_code")) if x) or o.get("address") or ""
    rows = [
        ("Order", o["id"]),
        ("Bouquet", what),
        ("Recipient", o.get("recipient_name") or ""),
        ("Phone", o.get("recipient_phone") or ""),
        ("Address", addr),
        ("Deliver", when),
        ("Instructions", o.get("delivery_instructions") or ""),
        ("Card message", o.get("message") or "(no card message)"),
        ("From customer", o.get("customer_name") or ""),
    ]
    trs = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#5c7a54;white-space:nowrap;vertical-align:top">{k}</td>'
        f'<td style="padding:6px 0">{v}</td></tr>'
        for k, v in rows if v
    )
    return f'<table style="border-collapse:collapse;font-size:0.95em">{trs}</table>'


def _notify_paid_order(o: dict) -> None:
    """Email the fulfilling florist (and the admin) a paid order's full details, once."""
    if o.get("florist_notified_at"):
        return
    details = _order_details_html(o)
    partner_email = None
    if o.get("partner_id"):
        with _db() as conn:
            p = conn.execute("SELECT email FROM partners WHERE id = ?", (o["partner_id"],)).fetchone()
        if p:
            partner_email = p["email"]
    sent = False
    if partner_email:
        sent = _send_email(partner_email, f"New paid order {o['id']} — ready to make & deliver 🌸", _email_shell(
            "<p><strong>You have a new paid order.</strong> Payment has cleared — "
            "everything you need is below. Please confirm the flowers can be delivered as requested.</p>"
            + details
        ))
    # admin always gets a copy (directory florists have no partner account yet)
    admin_sent = _send_email(
        ADMIN_EMAIL,
        f"[yo.florist] paid order {o['id']} → {o.get('florist')}"
        + ("" if partner_email else " (manual handoff needed)"),
        _email_shell(f"<p>Routed to <strong>{o.get('florist')}</strong>"
                     f"{f' (partner, emailed {partner_email})' if sent else ' — no partner email, engage manually'}.</p>"
                     + details),
    )
    if sent or admin_sent:
        with _db() as conn:
            conn.execute("UPDATE orders SET florist_notified_at = ? WHERE id = ?",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"), o["id"]))


# ─── admin: shop moderation (yo.florist/admin) ───

def _admin_auth(x_admin_key: str = Header(...)):
    if not ADMIN_KEY or not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid admin key")


@app.get("/v1/admin/stats", include_in_schema=False, dependencies=[Depends(_admin_auth)])
def admin_stats():
    with _db() as conn:
        partners = dict(conn.execute("SELECT status, COUNT(*) FROM partners GROUP BY status").fetchall())
        items = conn.execute("SELECT COUNT(*) FROM partner_items WHERE status='active'").fetchone()[0]
        orders = dict(conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
    return {"partners": partners, "partner_items": items, "orders": orders}


@app.get("/v1/admin/partners", include_in_schema=False, dependencies=[Depends(_admin_auth)])
def admin_partners(status: str = Query("pending")):
    q = ("SELECT p.*, (SELECT COUNT(*) FROM partner_items pi WHERE pi.partner_id = p.id"
         " AND pi.status='active') AS item_count FROM partners p")
    args: tuple = ()
    if status != "all":
        q += " WHERE p.status = ?"
        args = (status,)
    q += " ORDER BY p.created_at DESC"
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    for r in rows:
        r.pop("key_hash", None)
    return {"count": len(rows), "partners": rows}


@app.get("/v1/admin/partners/{pid}/items", include_in_schema=False, dependencies=[Depends(_admin_auth)])
def admin_partner_items(pid: str):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, status, title, price, currency, image, url, description"
            " FROM partner_items WHERE partner_id = ? ORDER BY created_at DESC", (pid,)).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


@app.post("/v1/admin/partners/{pid}/status", include_in_schema=False, dependencies=[Depends(_admin_auth)])
def admin_partner_set_status(pid: str, status: str = Query(..., description="active | rejected | suspended | pending")):
    if status not in ("active", "rejected", "suspended", "pending"):
        raise HTTPException(status_code=422, detail="Invalid status")
    with _db() as conn:
        row = conn.execute("SELECT status, email, shop_name, iso2 FROM partners WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such partner")
        conn.execute("UPDATE partners SET status = ? WHERE id = ?", (status, pid))
    if status == "active" and row["status"] != "active":
        _notify_partner_approved(row["email"], row["shop_name"], row["iso2"])
    return {"id": pid, "status": status}


@app.get("/v1/admin/orders", include_in_schema=False, dependencies=[Depends(_admin_auth)])
def admin_orders(limit: int = Query(50, ge=1, le=200)):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, status, iso2, florist, partner_id, customer_name, email,"
            " recipient_name, recipient_phone, street, city, postal_code, address,"
            " delivery_instructions, message, delivery_date, delivery_time,"
            " confirm_time_with_customer, florist_notified_at,"
            " item_title, item_price, item_currency, budget_usd"
            " FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"count": len(rows), "orders": [dict(r) for r in rows]}


def _partner_isos() -> set[str]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.iso2 FROM partner_items pi JOIN partners p ON p.id = pi.partner_id"
            " WHERE pi.status = 'active' AND p.status = 'active'").fetchall()
    return {r[0] for r in rows}


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
        ("delivery_time", "TEXT"),
        ("confirm_time_with_customer", "INTEGER"),
        ("partner_id", "TEXT"),
        ("florist_notified_at", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS partners (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            key_hash TEXT NOT NULL,
            shop_name TEXT NOT NULL,
            iso2 TEXT NOT NULL,
            country TEXT NOT NULL,
            city TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            website TEXT,
            instagram TEXT,
            description TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS partner_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id TEXT NOT NULL REFERENCES partners(id),
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            title TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            image TEXT,
            url TEXT,
            description TEXT
        )"""
    )
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
            try:
                _notify_paid_order({**row, "status": "paid"})
            except Exception as exc:
                print(f"paid-order notify error for {row['id']}: {exc}", flush=True)
            return "paid"
    except requests.RequestException:
        pass
    return status


def _sweep_pending_payments() -> dict:
    """Poll Stripe for recent unpaid orders so a customer who pays and closes the
    tab (never landing on thanks.html) still triggers the florist handoff email."""
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    with _db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE status = 'pending_payment'"
            " AND stripe_session_id IS NOT NULL AND created_at >= ?", (cutoff_iso,))]
    paid = sum(1 for row in rows if _refresh_payment_status(row) == "paid")
    return {"checked": len(rows), "newly_paid": paid}


@app.post("/v1/admin/sweep-payments", include_in_schema=False, dependencies=[Depends(_admin_auth)])
def admin_sweep_payments():
    return _sweep_pending_payments()


@app.on_event("startup")
def _start_payment_sweeper():
    if not STRIPE_SECRET_KEY:
        return

    def loop():
        while True:
            time.sleep(600)
            try:
                _sweep_pending_payments()
            except Exception as exc:
                print(f"payment sweep error: {exc}", flush=True)

    threading.Thread(target=loop, daemon=True).start()


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
    delivery_time: str | None = Field(None, max_length=40, description="Preferred window, e.g. 'morning (9am–12pm)'")
    confirm_time_with_customer: bool = Field(False, description="Florist should phone the recipient to agree the exact time")
    partner_id: str | None = Field(None, max_length=20, description="Route to this partner shop instead of the country's directory florist")


@app.post("/v1/orders", status_code=201, tags=["orders"], summary="Create a purchase order (payment activates when Stripe is approved)")
def create_order(order: OrderIn):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", order.email):
        raise HTTPException(status_code=422, detail="Invalid email address")
    if order.partner_id:
        with _db() as conn:
            p = conn.execute("SELECT * FROM partners WHERE id = ? AND status = 'active'",
                             (order.partner_id,)).fetchone()
        if p is None:
            raise HTTPException(status_code=404, detail="Unknown partner shop")
        florist = {"iso2": p["iso2"], "country": p["country"], "name": p["shop_name"],
                   "city": p["city"], "website": p["website"] or p["instagram"] or ""}
    else:
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
            " delivery_instructions, lat, lng, delivery_time, confirm_time_with_customer, partner_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                order.delivery_time,
                int(order.confirm_time_with_customer),
                order.partner_id,
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
        "delivery": {
            "date": order.delivery_date,
            "time": order.delivery_time,
            "confirm_time_with_customer": order.confirm_time_with_customer,
        },
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
