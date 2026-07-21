"""Flower Aggregator API — serves the partner-florist directory behind yo.florist."""
import json
import unicodedata
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

DATA_FILE = Path("/opt/yoflorist/florists.json")

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
