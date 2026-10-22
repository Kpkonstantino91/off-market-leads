"""Do-not-contact rules: National Do Not Call flags from skip tracing, TCPA litigators, and our own
suppression list (NY DOS cease-and-desist owners, people who asked to be left alone)."""
from sqlalchemy import select

from . import db, normalize

# NY DOS cease-and-desist zones bar real estate solicitation of listed homeowners in the zone.
# As of Oct 2026 research, the only active Brooklyn zone is Community District 17 (East Flatbush),
# outside the default neighborhoods. Owners who are on a list can be added on the Settings page.
CEASE_DESIST_NOTE = ("Only NY DOS cease-and-desist zone found in Brooklyn: Community District 17 (East Flatbush), "
                     "outside your neighborhoods. Check dos.ny.gov before mailing a new area.")


def suppressed_set(conn):
    out = {}
    for r in conn.execute(select(db.suppression.c.kind, db.suppression.c.norm, db.suppression.c.reason)):
        out[(r.kind, r.norm)] = r.reason
    return out


def check(sup, kind, value):
    """Reason string if this phone/email/name/address is on the suppression list, else None."""
    return sup.get((kind, normalize.suppression_norm(kind, value)))


def listing_suppressed(sup, listing, owner_names=()):
    a = normalize.suppression_norm("address", f"{listing['address']} #{listing['unit']}" if listing.get("unit")
                                   else listing["address"])
    if ("address", a) in sup:
        return sup[("address", a)]
    base = normalize.suppression_norm("address", listing["address"])
    if ("address", base) in sup:
        return sup[("address", base)]
    for n in owner_names:
        r = check(sup, "name", n)
        if r:
            return r
    return None
