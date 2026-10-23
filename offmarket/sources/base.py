"""The shape every listing source hands to the pipeline, and status / type clean-up shared by sources."""
import re
from datetime import datetime

from .. import normalize

# Normalised statuses
ACTIVE, PENDING, CLOSED = "Active", "Pending", "Closed"
EXPIRED, WITHDRAWN, CANCELED, OFF_MARKET, UNKNOWN = "Expired", "Withdrawn", "Canceled", "Off market", "Unknown"
REMOVED = "Removed"  # off the market, reason not given by the source
ON_MARKET = {ACTIVE, PENDING}
PULLED = {EXPIRED, WITHDRAWN, CANCELED, OFF_MARKET, REMOVED}

_STATUS = [
    (r"closed|sold|leased|rented", CLOSED),
    (r"pending|contract|under ?contract|accepted ?offer|contingent|in ?escrow|ActiveUnderContract", PENDING),
    (r"expired", EXPIRED),
    (r"withdrawn|withdraw", WITHDRAWN),
    (r"cancel", CANCELED),
    (r"off ?market|temp|hold|tom|incomplete|delete", OFF_MARKET),
    (r"active|coming ?soon|new|back on market|price change|open", ACTIVE),
]


def status(s):
    s = (s or "").strip()
    if not s:
        return UNKNOWN
    for pat, val in _STATUS:
        if re.search(pat, s, re.I):
            return val
    return UNKNOWN


_TYPES = [
    (r"co-?op|cooperative|stock ?coop", "Co-op"),
    (r"condo|condop", "Condo"),
    (r"town ?house|townhome|brownstone|row ?house", "Townhouse"),
    (r"multi|2-4|two family|three family|duplex|triplex|residentialincome|income", "Multi-family"),
    (r"single ?family|house|detached|singlefamily", "Single family"),
    (r"rent|lease", "Rental"),
]


def property_type(*vals):
    for v in vals:
        if not v:
            continue
        for pat, val in _TYPES:
            if re.search(pat, v, re.I):
                return val
    return "Other"


def borough_from(city=None, county=None, zip_code=None):
    c = f"{city or ''} {county or ''}".lower()
    if "manhattan" in c or "new york county" in c or c.strip() in ("new york", "new york new york"):
        return "Manhattan"
    if "brooklyn" in c or "kings" in c:
        return "Brooklyn"
    if "queens" in c:
        return "Queens"
    if "bronx" in c:
        return "Bronx"
    if "staten" in c or "richmond" in c:
        return "Staten Island"
    z = (zip_code or "")[:3]
    return {"100": "Manhattan", "101": "Manhattan", "102": "Manhattan", "112": "Brooklyn", "104": "Bronx",
            "103": "Staten Island", "110": "Queens", "111": "Queens", "113": "Queens", "114": "Queens",
            "116": "Queens"}.get(z, "")


def parse_date(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:26] if "%f" in fmt else s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def bbl_from(parcel):
    d = re.sub(r"\D", "", parcel or "")
    if len(d) == 10 and d[0] in "12345":
        return d
    return None


def listing(**kw):
    """Fill in defaults and derived fields so every source returns the same keys."""
    out = dict(source="", listing_key="", url=None, address="", unit="", borough="", neighborhood="", zip="",
               property_type="Other", beds=None, baths=None, sqft=None, list_price=None, original_price=None,
               close_price=None, close_date=None, list_date=None, status=UNKNOWN, status_date=None, agent=None,
               office=None, agent_phone=None, agent_email=None, remarks=None, photo_url=None, bbl=None,
               lat=None, lon=None, days_on_market=None, raw=None)
    out.update({k: v for k, v in kw.items() if v is not None})
    if out["address"] and not out["unit"]:
        out["address"], out["unit"] = normalize.split_unit(out["address"])
    out["zip"] = (out["zip"] or "")[:5]
    if not out["borough"]:
        out["borough"] = borough_from(zip_code=out["zip"])
    return out
