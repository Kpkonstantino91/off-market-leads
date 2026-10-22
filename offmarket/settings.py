"""Search settings the broker edits on the website (stored in the database)."""
from sqlalchemy import select

from . import db

# Default "nice parts of Brooklyn", with the ZIP codes used when a listing has no neighborhood name.
BROOKLYN_DEFAULT = {
    "Brooklyn Heights": ["11201"],
    "DUMBO": ["11201"],
    "Cobble Hill": ["11201", "11231"],
    "Carroll Gardens": ["11231"],
    "Boerum Hill": ["11201", "11217"],
    "Park Slope": ["11215", "11217"],
    "Prospect Heights": ["11238"],
    "Fort Greene": ["11205", "11217"],
    "Clinton Hill": ["11205", "11238"],
    "Williamsburg": ["11211", "11249"],
    "Greenpoint": ["11222"],
}

DEFAULTS = {
    "min_price": 5_000_000,
    # Count a listing if its highest asking price was over the floor, even if it was later cut below it.
    "use_high_price": True,
    "manhattan_all": True,
    "brooklyn_neighborhoods": BROOKLYN_DEFAULT,
    "property_types": ["Condo", "Co-op", "Townhouse", "Single family", "Multi-family"],
    # A recorded deed/co-op transfer counts as "sold" when its price is at least this share of the last
    # asking price (keeps $0 transfers into a trust or LLC from hiding a lead).
    "sale_min_ratio": 0.5,
    # Keep re-checking pulled listings for a late-recorded sale for this many days.
    "sale_recheck_days": 365,
    # A listing that was in contract and then vanished becomes a lead after this many days with no recorded sale.
    "contract_wait_days": 120,
    # Days a listing must be missing from the active feed before we call it "disappeared".
    "missing_days": 2,
    # Skip trace new leads automatically (costs money per match) for these tiers. Empty = manual only.
    "auto_skiptrace_tiers": [],
}


def get_all(conn=None):
    own = conn is None
    if own:
        conn = db.engine().connect()
    try:
        stored = {r.key: r.value for r in conn.execute(select(db.settings))}
    finally:
        if own:
            conn.close()
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return out


def save(values):
    with db.engine().begin() as conn:
        for k, v in values.items():
            if k not in DEFAULTS:
                continue
            conn.execute(db.settings.delete().where(db.settings.c.key == k))
            conn.execute(db.settings.insert().values(key=k, value=v))


def in_area(s, borough, neighborhood, zip_code):
    """Is this listing in Manhattan (all) or one of the chosen Brooklyn neighborhoods?"""
    b = (borough or "").lower()
    z = (zip_code or "")[:5]
    if b == "manhattan" or (not b and z[:3] in ("100", "101", "102")):
        return bool(s["manhattan_all"])
    if b == "brooklyn" or (not b and z.startswith("112")):
        hoods = s["brooklyn_neighborhoods"]
        n = (neighborhood or "").strip().lower()
        if n:
            for name in hoods:
                if name.lower() == n or name.lower() in n or n in name.lower():
                    return True
            # A named neighborhood that isn't on the list (e.g. "Bushwick") is out even if the ZIP overlaps.
            if _known_brooklyn_name(n):
                return False
        return any(z in zips for zips in hoods.values())
    return False


def area_label(s, borough, neighborhood, zip_code):
    if neighborhood:
        return neighborhood
    if (borough or "").lower() == "brooklyn":
        for name, zips in s["brooklyn_neighborhoods"].items():
            if (zip_code or "")[:5] in zips:
                return name
    return borough or ""


_OTHER_BK = {"bushwick", "bed-stuy", "bedford-stuyvesant", "crown heights", "red hook", "gowanus",
             "windsor terrace", "prospect lefferts", "flatbush", "east williamsburg", "bay ridge",
             "sunset park", "ditmas park", "kensington", "columbia street waterfront", "vinegar hill",
             "navy yard", "south slope", "brooklyn navy yard"}


def _known_brooklyn_name(n):
    return n in _OTHER_BK
