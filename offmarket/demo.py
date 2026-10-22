"""SAMPLE data so the site can be tried without a feed. Every owner, phone and email here is made up."""
import random
from datetime import timedelta

from . import db, pipeline, settings
from .sources import base

STREETS = [
    ("Manhattan", "Upper East Side", "10021", "Park Avenue", "Co-op"), ("Manhattan", "Tribeca", "10013", "Franklin Street", "Condo"),
    ("Manhattan", "West Village", "10014", "Perry Street", "Townhouse"), ("Manhattan", "Upper West Side", "10024", "Riverside Drive", "Co-op"),
    ("Manhattan", "SoHo", "10012", "Greene Street", "Condo"), ("Manhattan", "Chelsea", "10011", "West 22nd Street", "Townhouse"),
    ("Manhattan", "Lenox Hill", "10065", "East 64th Street", "Co-op"), ("Manhattan", "Flatiron", "10010", "East 20th Street", "Condo"),
    ("Manhattan", "Hudson Yards", "10001", "Hudson Yards", "Condo"), ("Manhattan", "Greenwich Village", "10011", "Fifth Avenue", "Co-op"),
    ("Manhattan", "Carnegie Hill", "10128", "East 93rd Street", "Townhouse"), ("Manhattan", "Lincoln Square", "10023", "Central Park West", "Condo"),
    ("Brooklyn", "Brooklyn Heights", "11201", "Columbia Heights", "Townhouse"), ("Brooklyn", "Park Slope", "11215", "Prospect Park West", "Townhouse"),
    ("Brooklyn", "DUMBO", "11201", "Water Street", "Condo"), ("Brooklyn", "Cobble Hill", "11201", "Clinton Street", "Townhouse"),
    ("Brooklyn", "Williamsburg", "11249", "Kent Avenue", "Condo"), ("Brooklyn", "Fort Greene", "11217", "South Oxford Street", "Townhouse"),
    ("Brooklyn", "Carroll Gardens", "11231", "President Street", "Townhouse"), ("Brooklyn", "Clinton Hill", "11205", "Washington Avenue", "Townhouse"),
    ("Brooklyn", "Greenpoint", "11222", "Eagle Street", "Condo"), ("Brooklyn", "Prospect Heights", "11238", "Prospect Place", "Townhouse"),
]
FIRST = ["Jordan", "Avery", "Morgan", "Riley", "Casey", "Taylor", "Quinn", "Reese", "Harper", "Rowan", "Emerson", "Sage"]
LAST = ["Sample", "Example", "Placeholder", "Demo", "Fictional", "Testwell"]
OFFICES = ["Example Realty", "Sample Sotheby's-style Brokerage", "Demo Partners", "Placeholder Group"]
STATUSES = [base.EXPIRED, base.WITHDRAWN, base.CANCELED, base.OFF_MARKET]


def load(seed=7):
    rnd = random.Random(seed)
    s = settings.get_all()
    today = db.now().replace(hour=11, minute=0, second=0, microsecond=0)
    rows_day1, rows_day2 = [], []
    for i, (boro, hood, z, street, ptype) in enumerate(STREETS * 2):
        num = 7 + i * 13 + rnd.randint(0, 6)
        unit = "" if ptype == "Townhouse" else rnd.choice(["PH", "12A", "8B", "21C", "5", "3F", "14W"])
        price = rnd.choice([5.25, 5.95, 6.5, 7.2, 8.9, 9.75, 11.5, 13.9, 16.5, 22.0, 29.5]) * 1e6
        listed = today - timedelta(days=rnd.randint(120, 600))
        common = dict(source="csv", listing_key=f"SAMPLE-{i}", address=f"{num} {street}", unit=unit, borough=boro,
                      neighborhood=hood, zip=z, property_type=ptype, beds=rnd.randint(2, 6), baths=rnd.randint(2, 6),
                      sqft=rnd.randint(1800, 7000), list_date=listed, agent=f"{rnd.choice(FIRST)} Agent",
                      office=rnd.choice(OFFICES), original_price=int(price))
        rows_day1.append(base.listing(**common, list_price=int(price), status=base.ACTIVE))
        cut = int(price * rnd.choice([1, 1, 0.95, 0.9, 0.85]))
        roll = rnd.random()
        st = base.ACTIVE if roll < 0.2 else base.CLOSED if roll < 0.28 else rnd.choice(STATUSES)
        off = today - timedelta(days=rnd.choice([1, 2, 4, 6, 9, 15, 24, 40, 70, 120]))
        rows_day2.append(base.listing(**common, list_price=cut, status=st, status_date=off,
                                      close_price=int(cut * 0.96) if st == base.CLOSED else None))
    with db.engine().begin() as conn:
        pipeline.ingest(conn, rows_day1, s, today=today - timedelta(days=200))
        pipeline.ingest(conn, rows_day2, s, today=today)
        L = db.listings.c
        from sqlalchemy import select
        for l in conn.execute(select(db.listings).where(L.state == "pulled")).mappings().all():
            ent = rnd.random() < 0.3
            name = f"{l['address'].split(' ', 1)[1].split()[0].title()} Sample Holdings LLC" if ent else \
                f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
            acq = l["list_date"] - timedelta(days=rnd.randint(900, 5000))
            conn.execute(db.owners.insert().values(
                listing_id=l["id"], name=name, is_entity=ent,
                mailing_address=f"{l['address'].upper()}{' APT ' + l['unit'] if l['unit'] else ''}, NEW YORK, NY",
                acquired_date=acq, acquired_price=int((l["high_price"] or 0) * 0.7), doc_id=f"SAMPLE{l['id']:06d}",
                doc_type="RPTT" if l["property_type"] == "Co-op" else "DEED",
                entity_people=[{"role": "Process address (DOS)", "name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
                                "address": "500 Fifth Ave, New York, NY 10110"}] if ent else None, source="ACRIS"))
            conn.execute(db.sale_docs.insert().values(
                listing_id=l["id"], doc_id=f"SAMPLE{l['id']:06d}", doc_type="RPTT" if l["property_type"] == "Co-op" else "DEED",
                doc_date=acq, amount=int((l["high_price"] or 0) * 0.7), unit=l["unit"], parties_to=name,
                counts_as_sale=False, source="ACRIS"))
            conn.execute(db.listings.update().where(L.id == l["id"]).values(
                sale_checked_at=today, owner_checked_at=today,
                sale_check_note=f"No sale recorded since {l['list_date']:%b %d, %Y} (SAMPLE data)."))
            if rnd.random() < 0.55:
                conn.execute(db.listings.update().where(L.id == l["id"]).values(skiptraced_at=today))
                for j in range(rnd.randint(1, 3)):
                    conn.execute(db.contacts.insert().values(
                        listing_id=l["id"], person=name if not ent else f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
                        kind="phone", value=f"212555{rnd.randint(1000, 1999)}", phone_type=rnd.choice(["Mobile", "Landline"]),
                        dnc=rnd.random() < 0.35, tcpa=False, suppressed=False, source="sample", created_at=today))
                if rnd.random() < 0.6:
                    conn.execute(db.contacts.insert().values(
                        listing_id=l["id"], person=name, kind="email", value=f"owner{l['id']}@example.com",
                        dnc=False, tcpa=False, suppressed=False, source="sample", created_at=today))
        pipeline.rescore(conn)
    print("Loaded SAMPLE data. Owners, phones and emails are fictional.")
