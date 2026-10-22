"""Other free public records: DOF rolling sales (second sold check), NYC GeoSearch (BBL and map point),
and NY DOS corporation records (who is behind an LLC owner)."""
import logging

import requests

from .. import normalize, socrata
from ..sources.base import parse_date

log = logging.getLogger(__name__)

# NYC Citywide Rolling Calendar Sales (DOF, last 12 months, includes co-op sales with apartment numbers).
ROLLING_SALES = "usep-8jbt"
# NY DOS active corporations & LLCs (process address, CEO / registered agent).
DOS_CORPS = "n9v6-gdp6"


def dof_sales(bbl, unit, since):
    """DOF sales on this tax lot since `since`; filtered to the unit when the lot is a co-op building."""
    if not bbl:
        return []
    b, blk, lot = bbl[0], int(bbl[1:6]), int(bbl[6:])
    rows = socrata.query("data.cityofnewyork.us", ROLLING_SALES,
                         where=f"borough='{b}' AND block='{blk}' AND lot='{lot}'", limit=500)
    out = []
    want = normalize.unit(unit)
    for r in rows:
        d = parse_date(r.get("sale_date"))
        if not d or (since and d < since):
            continue
        apt = normalize.unit(r.get("apartment_number"))
        if want and lot < 1001 and apt and apt != want:
            continue
        out.append({"doc_id": f"DOF-{bbl}-{d:%Y%m%d}-{apt or 'x'}", "doc_type": "DOF SALE", "doc_date": d,
                    "recorded": None, "amount": normalize.price(r.get("sale_price")) or 0,
                    "unit": r.get("apartment_number") or "", "from": [], "to": []})
    return out


def geosearch(address, borough):
    """NYC Planning GeoSearch (free, no key): base BBL and lat/lon for a street address."""
    try:
        r = requests.get("https://geosearch.planninglabs.nyc/v2/search",
                         params={"text": f"{address}, {borough}, NY", "size": 1}, timeout=30)
        r.raise_for_status()
        feats = r.json().get("features") or []
    except (requests.RequestException, ValueError) as e:
        log.warning("geosearch failed for %s: %s", address, e)
        return None
    if not feats:
        return None
    f = feats[0]
    pad = (f.get("properties", {}).get("addendum") or {}).get("pad") or {}
    lon, lat = f.get("geometry", {}).get("coordinates", [None, None])
    return {"bbl": pad.get("bbl"), "lat": lat, "lon": lon}


def llc_people(entity_name):
    """Process address, CEO and registered agent for an LLC / corp from NY DOS."""
    n = (entity_name or "").upper().replace("'", "''").strip()
    if not n:
        return []
    try:
        rows = list(socrata.query("data.ny.gov", DOS_CORPS, where=f"upper(current_entity_name)='{n}'", limit=3))
    except Exception as e:  # noqa: BLE001 - enrichment is best-effort
        log.warning("DOS lookup failed for %s: %s", entity_name, e)
        return []
    people = []
    for r in rows:
        for role, nm, a1, city, st, z in [
            ("Process address (DOS)", "dos_process_name", "dos_process_address_1", "dos_process_city",
             "dos_process_state", "dos_process_zip"),
            ("CEO", "ceo_name", "ceo_address_1", "ceo_city", "ceo_state", "ceo_zip"),
            ("Registered agent", "registered_agent_name", "registered_agent_address_1", "registered_agent_city",
             "registered_agent_state", "registered_agent_zip"),
        ]:
            if r.get(nm) or r.get(a1):
                addr = ", ".join(x for x in [r.get(a1), r.get(city), " ".join(y for y in [r.get(st), r.get(z)] if y)]
                                 if x)
                people.append({"role": role, "name": normalize.person_name(r.get(nm, "")), "address": addr})
    return people
