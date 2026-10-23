"""End to end with fixture data: listings come in, some come off, public records decide which sold."""
import re
from datetime import datetime

import pytest

from offmarket import config, db, pipeline, settings
from offmarket.enrich import skiptrace
from offmarket.public import acris, records
from offmarket.sources import csv_import

DAY1 = datetime(2026, 6, 1, 11)
DAY2 = datetime(2026, 8, 3, 11)
DAY3 = datetime(2026, 10, 20, 11)

SNAPSHOT = b"""Listing ID,Address,Unit,Neighborhood,Zip,Borough,Type,Beds,Baths,List Price,Original Price,List Date,Agent,Office,Status
A1,15 Central Park West,20A,Lincoln Square,10023,Manhattan,Condo,3,3.5,"$7,500,000","$8,200,000",2025-11-01,Pat Lee,Big Brokerage,Active
A2,12 Willow Street,,Brooklyn Heights,11201,Brooklyn,Townhouse,5,4,6200000,,2026-03-01,Sam Roe,Heights Realty,Active
A3,820 Park Avenue,12B,Upper East Side,10021,Manhattan,Co-op,4,4,9000000,,2026-02-10,Ann Fox,Park Partners,Active
A4,100 Bushwick Avenue,PH,Bushwick,11206,Brooklyn,Condo,3,3,6000000,,2026-04-01,,,Active
A5,1 Main St,5,Astoria,11102,Queens,Condo,3,2,7000000,,2026-04-01,,,Active
A6,200 East 72nd Street,9C,Lenox Hill,10021,Manhattan,Co-op,2,2,4000000,,2026-04-01,,,Active
A7,10 East 72nd Street,,Upper East Side,10021,Manhattan,Townhouse,6,6,25000000,,2025-09-01,Kim Wu,Elite,Active
"""

DAY2_STATUS = b"""Listing ID,Address,Unit,Zip,Borough,Neighborhood,Type,List Price,Status,Status Date
A1,15 Central Park West,20A,10023,Manhattan,Lincoln Square,Condo,"6,900,000",Expired,2026-07-30
A2,12 Willow Street,,11201,Brooklyn,Brooklyn Heights,Townhouse,6200000,Withdrawn,2026-07-15
A3,820 Park Avenue,12B,10021,Manhattan,Upper East Side,Co-op,9000000,In Contract,2026-07-01
A7,10 East 72nd Street,,10021,Manhattan,Upper East Side,Townhouse,25000000,Temporarily Off Market,2026-07-20
"""

DAY3_STATUS = b"""Listing ID,Address,Unit,Zip,Borough,Neighborhood,Type,List Price,Status,Status Date
A3,820 Park Avenue,12B,10021,Manhattan,Upper East Side,Co-op,9000000,Withdrawn,2026-08-20
B1,15 Central Park West,20A,10023,Manhattan,Lincoln Square,Condo,6500000,Active,2026-10-15
"""

# --- fake public records -------------------------------------------------------------------------
LEGALS = [
    {"document_id": "D-CPW-2019", "borough": "1", "block": "1115", "lot": "1205", "street_number": "15",
     "street_name": "CENTRAL PARK WEST", "unit": "20A"},
    {"document_id": "D-WILLOW-2014", "borough": "3", "block": "214", "lot": "12", "street_number": "12",
     "street_name": "WILLOW STREET", "unit": ""},
    {"document_id": "D-WILLOW-2026", "borough": "3", "block": "214", "lot": "12", "street_number": "12",
     "street_name": "WILLOW STREET", "unit": ""},
    {"document_id": "D-PARK-2026", "borough": "1", "block": "1386", "lot": "1", "street_number": "820",
     "street_name": "PARK AVENUE", "unit": "12B"},
    {"document_id": "D-PARK-OTHER", "borough": "1", "block": "1386", "lot": "1", "street_number": "820",
     "street_name": "PARK AVENUE", "unit": "3A"},
    {"document_id": "D-E72-2010", "borough": "1", "block": "1385", "lot": "60", "street_number": "10",
     "street_name": "EAST   72ND STREET", "unit": ""},
    {"document_id": "D-E72-TRUST", "borough": "1", "block": "1385", "lot": "60", "street_number": "10",
     "street_name": "EAST 72 STREET", "unit": ""},
]
MASTER = {
    "D-CPW-2019": ("DEED", "2019-05-02", "6100000"),
    "D-WILLOW-2014": ("DEED", "2014-01-10", "3000000"),
    "D-WILLOW-2026": ("DEED", "2026-09-30", "6000000"),
    "D-PARK-2026": ("RPTT", "2026-09-15", "8800000"),
    "D-PARK-OTHER": ("RPTT", "2026-09-01", "2000000"),
    "D-E72-2010": ("DEED", "2010-03-01", "14000000"),
    "D-E72-TRUST": ("DEED", "2026-08-15", "0"),
}
PARTIES = [
    ("D-CPW-2019", "1", "OLD OWNER, ALAN", ""), ("D-CPW-2019", "2", "SMITH, JOHN", "15 CENTRAL PARK W APT 20A"),
    ("D-WILLOW-2014", "2", "ROE, MARY", ""), ("D-WILLOW-2026", "2", "BUYER, NEW", ""),
    ("D-PARK-2026", "2", "COOP BUYER, PAT", ""),
    ("D-E72-2010", "2", "WU, KIM", ""), ("D-E72-TRUST", "1", "WU, KIM", ""),
    ("D-E72-TRUST", "2", "EAST 72 HOLDINGS LLC", "500 FIFTH AVE STE 900"),
]


def fake_query(domain, dataset, where=None, **kw):
    if dataset == acris.LEGALS:
        num = re.search(r"street_number='([^']+)'", where or "")
        b = re.search(r"borough=(\d)", where or "")
        return [r for r in LEGALS if (not num or r["street_number"] == num.group(1)) and
                (not b or r["borough"] == b.group(1))]
    ids = re.findall(r"'(D-[A-Z0-9-]+)'", where or "")
    if dataset == acris.MASTER:
        return [{"document_id": i, "doc_type": MASTER[i][0], "document_date": MASTER[i][1] + "T00:00:00.000",
                 "document_amt": MASTER[i][2]} for i in ids if i in MASTER]
    if dataset == acris.PARTIES:
        return [{"document_id": d, "party_type": t, "name": n, "address_1": a, "city": "NEW YORK", "state": "NY"}
                for d, t, n, a in PARTIES if d in ids]
    if dataset == records.DOS_CORPS:
        return [{"current_entity_name": "EAST 72 HOLDINGS LLC", "dos_process_name": "KIM WU",
                 "dos_process_address_1": "500 FIFTH AVE STE 900", "dos_process_city": "NEW YORK",
                 "dos_process_state": "NY"}]
    return []


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    db.reset_engine()
    monkeypatch.setattr("offmarket.socrata.query", fake_query)
    monkeypatch.setattr(records, "geosearch", lambda a, b: None)
    yield
    db.reset_engine()


def _import(data, day, snapshot=False):
    rows, _ = csv_import.parse(data)
    s = settings.get_all()
    with db.engine().begin() as conn:
        st = pipeline.ingest(conn, rows, s, today=day)
        if snapshot:
            st["missing"] = pipeline.mark_missing(conn, "csv", {r["listing_key"] for r in rows}, s, today=day)
    return st


def _get(key):
    with db.engine().connect() as conn:
        from sqlalchemy import select
        return conn.execute(select(db.listings).where(db.listings.c.listing_key == key)).mappings().first()


def test_full_flow(env):
    st = _import(SNAPSHOT, DAY1)
    assert st["seen"] == 4          # Bushwick, Queens and the $4M co-op are out
    assert _get("A4") is None and _get("A5") is None and _get("A6") is None
    a1 = _get("A1")
    assert a1["high_price"] == 8_200_000 and a1["price_cuts"] == 1 and a1["neighborhood"] == "Lincoln Square"

    st = _import(DAY2_STATUS, DAY2)
    assert st["pulled"] == 3
    assert _get("A1")["state"] == "pulled" and _get("A1")["off_reason"] == "Expired"
    assert _get("A1")["price_cuts"] == 2
    assert _get("A3")["was_in_contract"]

    s = settings.get_all()
    with db.engine().begin() as conn:
        out = pipeline.check_sales(conn, s, today=DAY3)
    assert out["errors"] == 0
    # Condo: only the owner's 2019 purchase on record -> still a lead, owner John Smith.
    assert _get("A1")["state"] == "pulled"
    # Townhouse: deed recorded after it was withdrawn -> sold quietly off market, filtered out.
    a2 = _get("A2")
    assert a2["state"] == "sold" and a2["sold_price"] == 6_000_000 and a2["sold_source"] == "ACRIS"
    # $25M townhouse: a $0 transfer into an LLC doesn't count as a sale; owner becomes the LLC.
    assert _get("A7")["state"] == "pulled"

    with db.engine().connect() as conn:
        from sqlalchemy import select
        own = {r.listing_id: r for r in conn.execute(select(db.owners))}
    assert own[a1["id"]].name == "John Smith" and "CENTRAL PARK" in own[a1["id"]].mailing_address
    llc = own[_get("A7")["id"]]
    assert llc.is_entity and llc.name == "East 72 Holdings LLC"
    assert llc.entity_people[0]["name"] == "Kim Wu"

    # Co-op goes from contract to withdrawn -> waits; the RPTT for unit 12B shows it sold.
    _import(DAY3_STATUS, DAY3)
    a3 = _get("A3")
    assert a3["state"] == "contract_off"
    with db.engine().begin() as conn:
        pipeline.check_one(conn, a3, s, today=DAY3)
    assert _get("A3")["state"] == "sold" and _get("A3")["sold_price"] == 8_800_000

    # The condo comes back with a new listing id: old one is "relisted", new one remembers the failure.
    assert _get("A1")["state"] == "relisted"
    assert _get("B1")["prior_pulls"] == 1

    with db.engine().begin() as conn:
        pipeline.rescore(conn, today=DAY3)
    a7 = _get("A7")
    assert a7["tier"] in ("Hot", "Warm", "Watch") and any("temporarily" in w for _, w in a7["score_reasons"])


def test_disappeared_from_snapshot(env):
    _import(SNAPSHOT, DAY1, snapshot=True)
    later = datetime(2026, 6, 5, 11)
    rows = SNAPSHOT.decode().splitlines()
    smaller = "\n".join(r for r in rows if not r.startswith("A2,")).encode()
    st = _import(smaller, later, snapshot=True)
    assert st["missing"] == 1
    a2 = _get("A2")
    assert a2["state"] == "pulled" and a2["off_reason"] == "Disappeared"


def test_skiptrace_parse_and_flags():
    out = skiptrace.parse({"results": {"persons": [{
        "name": {"first": "John", "last": "Smith"},
        "phoneNumbers": [{"number": "212-555-0101", "type": "Mobile", "dnc": False, "tcpa": False},
                         {"number": "(917) 555-0199", "type": "Mobile", "dnc": True, "tcpa": False}],
        "emails": [{"email": "John@Example.com"}]}]}})
    assert [c["value"] for c in out] == ["2125550101", "9175550199", "john@example.com"]
    assert out[1]["dnc"] and not out[0]["dnc"]


def test_area_rules():
    s = settings.get_all()
    assert settings.in_area(s, "Manhattan", "", "10021")
    assert settings.in_area(s, "Brooklyn", "Park Slope", "11215")
    assert settings.in_area(s, "Brooklyn", "", "11222")             # Greenpoint ZIP
    assert not settings.in_area(s, "Brooklyn", "Bushwick", "11211")  # named, not on the list
    assert not settings.in_area(s, "Queens", "Astoria", "11102")


def test_pages_render(env, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_SCHEDULER", False)
    _import(SNAPSHOT, DAY1)
    _import(DAY2_STATUS, DAY2)
    with db.engine().begin() as conn:
        pipeline.check_sales(conn, settings.get_all(), today=DAY3)
        pipeline.rescore(conn, today=DAY3)
    from fastapi.testclient import TestClient
    from offmarket.web.app import app
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200 and "15 Central Park West" in r.text and "12 Willow" not in r.text
        assert c.get("/?view=sold").text.count("12 Willow Street") == 1
        lid = _get("A1")["id"]
        page = c.get(f"/listing/{lid}")
        assert page.status_code == 200 and "John Smith" in page.text
        assert c.get(f"/listing/{lid}/letter").status_code == 200
        assert c.get("/settings").status_code == 200 and c.get("/sources").status_code == 200
        assert "John Smith" in c.get("/export.csv").text
        r = c.post("/suppression", data={"kind": "name", "value": "John Smith", "reason": "Asked not to be contacted"})
        assert r.status_code == 200
        assert "Do not contact" in c.get(f"/listing/{lid}").text
        r = c.post("/import", files={"file": ("x.csv", DAY3_STATUS, "text/csv")})
        assert r.status_code == 200 and "Imported" in r.text


def test_reso_mapping():
    from offmarket.sources import reso
    l = reso.to_listing({
        "ListingKey": "RLS123", "StandardStatus": "Withdrawn", "MlsStatus": "Temporarily Off Market",
        "ListPrice": 6250000, "OriginalListPrice": 6995000, "UnparsedAddress": "200 East 66th Street",
        "UnitNumber": "PH-B", "City": "New York", "CountyOrParish": "New York", "PostalCode": "10065",
        "PropertySubType": "Cooperative", "PropertyType": "Residential", "WithdrawnDate": "2026-10-01",
        "ListingContractDate": "2026-01-15", "ParcelNumber": "1-01420-0021", "ListOfficeName": "X Realty"})
    assert l["status"] == "Off market" and l["borough"] == "Manhattan" and l["property_type"] == "Co-op"
    assert l["status_date"] == datetime(2026, 10, 1) and l["bbl"] == "1014200021" and l["unit"] == "PH-B"
    assert reso.to_listing({"ListingKey": "k", "StandardStatus": "ActiveUnderContract"})["status"] == "Pending"
    assert reso.to_listing({"ListingKey": "k", "StandardStatus": "Expired"})["status"] == "Expired"


def test_rentcast_mapping_and_ingest(env):
    from offmarket.sources import rentcast
    row = rentcast.to_listing({
        "id": "15-Central-Park-W,-Apt-20A,-New-York,-NY-10023", "addressLine1": "15 Central Park W",
        "addressLine2": "Apt 20A", "city": "New York", "state": "NY", "zipCode": "10023", "county": "New York",
        "propertyType": "Condo", "bedrooms": 3, "bathrooms": 3.5, "squareFootage": 2400, "status": "Inactive",
        "price": 6900000, "listedDate": "2025-11-01T00:00:00.000Z", "removedDate": "2026-07-30T00:00:00.000Z",
        "daysOnMarket": 271, "listingOffice": {"name": "Big Brokerage"}, "listingAgent": {"name": "Pat Lee"},
        "history": {"2025-11-01": {"event": "Sale Listing", "price": 8200000, "listedDate": "2025-11-01T00:00:00.000Z"}}})
    assert row["status"] == "Removed" and row["unit"] == "20A" and row["borough"] == "Manhattan"
    assert row["original_price"] == 8_200_000
    with db.engine().begin() as conn:
        st = pipeline.ingest(conn, [row], settings.get_all(), today=DAY3)
    assert st["pulled"] == 1
    l = _get(row["listing_key"])
    assert l["state"] == "pulled" and l["off_reason"] == "Removed" and l["high_price"] == 8_200_000
    assert rentcast._type("Apartment") == "Co-op"


def test_rentcast_monthly_cap(env, monkeypatch):
    from offmarket.sources import rentcast
    calls = []

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return []
    monkeypatch.setattr(config, "RENTCAST_API_KEY", "k")
    monkeypatch.setattr(config, "RENTCAST_MONTHLY_CAP", 2)
    monkeypatch.setattr(rentcast, "_get", lambda p: calls.append(p) or R())
    assert pipeline._step("rentcast", pipeline.run_rentcast)["requests"] == 1
    assert pipeline._step("rentcast", pipeline.run_rentcast)["requests"] == 1
    assert "limit reached" in pipeline._step("rentcast", pipeline.run_rentcast)["skipped"]
    assert len(calls) == 2 and pipeline.rentcast_requests_this_month() == 2


def test_sales_check_saves_per_listing(env):
    _import(SNAPSHOT, DAY1)
    _import(DAY2_STATUS, DAY2)
    out = pipeline.check_sales(None, settings.get_all(), today=DAY3, minutes=30)
    assert out["checked"] == 3 and out["sold"] == 1
    assert _get("A2")["state"] == "sold" and _get("A1")["owner_checked_at"] is not None
