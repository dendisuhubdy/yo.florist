"""Flower Aggregator API — serves the partner-florist directory behind yo.florist."""
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

DATA_FILE = Path("/opt/yoflorist/florists.json")
DB_FILE = Path("/opt/yoflorist/data/orders.db")

# TODO(stripe): set STRIPE_SECRET_KEY in the systemd unit once the Stripe
# account is approved; until then orders are stored as pending_payment and
# no payment session is created.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

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
    allow_methods=["GET"],
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
        "countries": [{"iso2": e["iso2"], "country": e["country"]} for e in data],
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
            matches.append(e)
    return {"query": q, "date": date, "count": len(matches), "matches": matches}


# ─── orders (Stripe payment pending account approval) ───

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
    return conn


def _find_florist(country: str) -> dict:
    q = _norm(country)
    for e in _load():
        if q == e["iso2"].lower() or q == _norm(e["country"]):
            return e
    raise HTTPException(status_code=404, detail=f"No partner florist found for {country!r}")


def _payment_block(order_id: str) -> dict:
    if STRIPE_SECRET_KEY:
        # TODO(stripe): once live keys are configured, create a real Checkout
        # Session here and return its URL:
        #   session = stripe.checkout.Session.create(
        #       mode="payment",
        #       line_items=[...bouquet + delivery, from the routed florist...],
        #       metadata={"order_id": order_id},
        #       success_url="https://yo.florist/thanks?order={CHECKOUT_SESSION_ID}",
        #       cancel_url="https://yo.florist/",
        #   )
        #   return {"provider": "stripe", "mode": "live", "checkout_url": session.url}
        raise HTTPException(status_code=501, detail="Stripe key present but checkout not wired yet")
    return {
        "provider": "stripe",
        "mode": "placeholder",
        "checkout_url": None,
        "note": (
            "Stripe account pending approval — no charge has been made. "
            "A secure payment link will be emailed when payments go live."
        ),
    }


class OrderIn(BaseModel):
    country: str = Field(..., description="ISO-3166 alpha-2 code or country name — the order routes to this country's partner florist")
    customer_name: str = Field(..., min_length=2, max_length=120)
    # NB: validated explicitly in create_order — Field(pattern=...) is silently
    # ignored by pydantic v1, which Ubuntu's apt-packaged FastAPI runs on.
    email: str = Field(...)
    address: str = Field(..., min_length=5, max_length=500, description="Recipient delivery address")
    delivery_date: str | None = Field(None, description="Requested delivery date, e.g. 2026-07-28")
    budget_usd: int | None = Field(None, ge=10, le=10000, description="Bouquet budget in USD, delivery included")
    message: str | None = Field(None, max_length=500, description="Card message for the recipient")


@app.post("/v1/orders", status_code=201, tags=["orders"], summary="Create a purchase order (payment activates when Stripe is approved)")
def create_order(order: OrderIn):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", order.email):
        raise HTTPException(status_code=422, detail="Invalid email address")
    florist = _find_florist(order.country)
    oid = "YF-" + uuid.uuid4().hex[:8].upper()
    with _db() as conn:
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )
    return {
        "order_id": oid,
        "status": "pending_payment",
        "routed_to": {
            "iso2": florist["iso2"],
            "country": florist["country"],
            "florist": florist["name"],
            "city": florist["city"],
        },
        "payment": _payment_block(oid),
    }


@app.get("/v1/orders/{order_id}", tags=["orders"], summary="Get an order's status")
def get_order(order_id: str):
    with _db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown order {order_id!r}")
    o = dict(row)
    o["payment"] = _payment_block(o["id"])
    # the customer-facing status endpoint doesn't echo back PII
    for k in ("email", "address", "customer_name", "message"):
        o.pop(k)
    return o
