"""Import listings from a spreadsheet export (RLS / Perchwell / OneKey / any CRM), uploaded on the website.

Column names are matched loosely ("Asking Price", "List Price", "price" all work). Two kinds of files:
  - a status export (has a Status column with Expired / Withdrawn / Sold ...): statuses are used directly;
  - a snapshot of what's active today: tick "full snapshot", and listings missing from it are treated
    as having left the market.
"""
import csv
import hashlib
import io

from .. import normalize
from . import base

name = "csv"
label = "Spreadsheet import"

ALIASES = {
    "listing_key": ["listing id", "listingid", "listing key", "listingkey", "mls", "mls #", "mls number", "rls id",
                    "web id", "listing number", "id"],
    "url": ["url", "link", "listing url", "listing link"],
    "address": ["address", "street address", "property address", "full address", "unparsedaddress"],
    "unit": ["unit", "apt", "apartment", "unit number", "unit #", "unitnumber"],
    "borough": ["borough", "city", "county"],
    "neighborhood": ["neighborhood", "neighbourhood", "area", "submarket", "subdivision", "subdivisionname"],
    "zip": ["zip", "zip code", "zipcode", "postal code", "postalcode"],
    "type": ["type", "property type", "ownership", "ownership type", "building type", "propertysubtype",
             "property sub type"],
    "beds": ["beds", "bedrooms", "br", "bedroomstotal"],
    "baths": ["baths", "bathrooms", "ba", "bathroomstotalinteger"],
    "sqft": ["sqft", "sq ft", "square feet", "approx sqft", "livingarea", "size"],
    "list_price": ["list price", "asking price", "price", "current price", "listprice"],
    "original_price": ["original price", "original list price", "originallistprice", "orig price"],
    "close_price": ["sold price", "close price", "closing price", "closeprice", "sale price"],
    "close_date": ["sold date", "close date", "closing date", "closedate"],
    "list_date": ["list date", "listed", "listing date", "date listed", "on market date", "listingcontractdate"],
    "status": ["status", "listing status", "standardstatus", "mlsstatus"],
    "status_date": ["status date", "off market date", "expiration date", "withdrawn date", "status change date",
                    "offmarketdate", "date off market"],
    "agent": ["agent", "listing agent", "list agent", "listagentfullname"],
    "office": ["office", "brokerage", "listing office", "firm", "listofficename"],
    "agent_phone": ["agent phone", "list agent phone"],
    "agent_email": ["agent email", "list agent email"],
    "bbl": ["bbl", "parcel", "parcel number", "parcelnumber"],
    "dom": ["dom", "days on market", "daysonmarket", "cdom"],
}


def _map_headers(headers):
    low = {h.strip().lower(): h for h in headers if h}
    out = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in low:
                out[field] = low[n]
                break
    return out


def parse(data: bytes, filename="upload.csv"):
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = _map_headers(reader.fieldnames or [])
    if "address" not in cols or "list_price" not in cols:
        raise ValueError("The file needs at least an address column and a price column. "
                         f"Found columns: {', '.join(reader.fieldnames or [])}")
    has_status = "status" in cols
    rows = []
    for r in reader:
        g = lambda f: (r.get(cols[f]) or "").strip() if f in cols else ""  # noqa: E731
        addr, unit = g("address"), g("unit")
        if not addr:
            continue
        if not unit:
            addr, unit = normalize.split_unit(addr)
        key = g("listing_key") or "addr-" + hashlib.sha1(
            f"{normalize.street(addr)}|{normalize.unit(unit)}".encode()).hexdigest()[:16]
        b = g("borough")
        rows.append(base.listing(
            source=name, listing_key=key, url=g("url") or None, address=addr, unit=unit,
            borough=base.borough_from(b, b, g("zip")) or b.title(), neighborhood=g("neighborhood"), zip=g("zip"),
            property_type=base.property_type(g("type")), beds=_num(g("beds")), baths=_num(g("baths")),
            sqft=normalize.price(g("sqft")), list_price=normalize.price(g("list_price")),
            original_price=normalize.price(g("original_price")), close_price=normalize.price(g("close_price")),
            close_date=base.parse_date(g("close_date")), list_date=base.parse_date(g("list_date")),
            status=base.status(g("status")) if has_status else base.ACTIVE,
            status_date=base.parse_date(g("status_date")), agent=g("agent") or None, office=g("office") or None,
            agent_phone=g("agent_phone") or None, agent_email=g("agent_email") or None,
            bbl=base.bbl_from(g("bbl")), days_on_market=normalize.price(g("dom")),
            raw={"file": filename, **{k: v for k, v in r.items() if k and v}},
        ))
    return rows, has_status


def _num(v):
    try:
        return float(v) if v else None
    except ValueError:
        return None
