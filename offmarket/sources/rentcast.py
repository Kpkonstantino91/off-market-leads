"""Listings from the RentCast property data API (rentcast.io): a licensed, paid-per-request feed that
covers US sale listings, including ones that came off the market ("Inactive", with a removed date).

RentCast assembles listings from public sources rather than the MLS, so NYC co-op coverage may be
thinner than the REBNY RLS feed. Sold listings also turn Inactive; the ACRIS / DOF sold check filters
those out.

Budget: the free Developer plan has 50 requests a month. One request returns up to 500 listings,
newest-seen first, and a single circle covers Manhattan and brownstone Brooklyn, so the default is one
request per daily run (about 30 a month). Raise RENTCAST_PAGES on a paid plan.
"""
import logging

import requests

from .. import config
from . import base

log = logging.getLogger(__name__)
name = "rentcast"
label = "RentCast listings API"
URL = "https://api.rentcast.io/v1/listings/sale"


def enabled():
    return bool(config.RENTCAST_API_KEY)


def _get(params):
    r = requests.get(URL, params=params, headers={"X-Api-Key": config.RENTCAST_API_KEY,
                                                  "Accept": "application/json"}, timeout=(15, 300))
    return r


def fetch(min_price, status="Inactive", budget=None, counter=None):
    """Listings over the floor inside the search circle, most recently seen first.
    Makes at most `budget` requests and adds each one to counter["requests"]."""
    counter = counter if counter is not None else {}
    counter.setdefault("requests", 0)
    budget = config.RENTCAST_PAGES if budget is None else min(budget, config.RENTCAST_PAGES)
    params = {"latitude": config.RENTCAST_LAT, "longitude": config.RENTCAST_LON, "radius": config.RENTCAST_RADIUS,
              "status": status, "price": f"{min_price}:*", "limit": config.RENTCAST_PAGE_SIZE}
    for page in range(budget):
        params["offset"] = page * config.RENTCAST_PAGE_SIZE
        r = _get(params)
        counter["requests"] += 1
        if r.status_code == 400 and params["price"].endswith("*") and counter["requests"] < budget:
            params["price"] = f"{min_price}:1000000000"   # older range syntax
            r = _get(params)
            counter["requests"] += 1
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("listings") or []
        for row in rows:
            yield to_listing(row)
        if len(rows) < config.RENTCAST_PAGE_SIZE:
            return


def _type(t):
    # RentCast has no co-op type; non-condo "Apartment" sale listings in NYC are almost always co-ops.
    if (t or "").lower() == "apartment":
        return "Co-op"
    return base.property_type(t)


def to_listing(r):
    g = r.get
    status = g("status") or ""
    st = base.ACTIVE if status.lower() == "active" else base.REMOVED
    hist = g("history") or {}
    listed = base.parse_date(g("listedDate"))
    # Earliest asking price in the current listing period, when history has it.
    original = None
    for _, h in sorted(hist.items()):
        if listed and base.parse_date(h.get("listedDate")) == listed and h.get("price"):
            original = original or h.get("price")
    agent, office = g("listingAgent") or {}, g("listingOffice") or {}
    city = g("city") or ""
    return base.listing(
        source=name, listing_key=str(g("id") or g("formattedAddress") or ""),
        address=g("addressLine1") or "", unit=(g("addressLine2") or "").replace("Apt", "").replace("Unit", "").strip(),
        borough=base.borough_from(city, g("county"), g("zipCode")), zip=g("zipCode") or "",
        property_type=_type(g("propertyType")), beds=g("bedrooms"), baths=g("bathrooms"),
        sqft=int(g("squareFootage")) if g("squareFootage") else None,
        list_price=_int(g("price")), original_price=_int(original), list_date=listed,
        status=st, status_date=base.parse_date(g("removedDate")) if st == base.REMOVED else None,
        agent=agent.get("name"), office=office.get("name"), agent_phone=agent.get("phone"),
        agent_email=agent.get("email"), lat=g("latitude"), lon=g("longitude"),
        days_on_market=g("daysOnMarket"), raw=r,
    )


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
