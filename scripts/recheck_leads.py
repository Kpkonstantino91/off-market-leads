"""One-off: re-check a set of leads (ids in recheck/ids.txt) for relisting and sales, and dump everything
public records say about their owners to recheck/out.json. Read-only on the leads, except that the one
RentCast request it makes is logged so it counts against the monthly cap.

  python scripts/recheck_leads.py
"""
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select  # noqa: E402

from offmarket import config, db, normalize, pipeline  # noqa: E402
from offmarket.public import acris, records  # noqa: E402
from offmarket.sources import rentcast  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recheck")
OUT = Path(__file__).resolve().parents[1] / "recheck"


def j(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    return v


def doc_out(d):
    return {k: j(v) for k, v in d.items()}


def main():
    ids = [int(x) for x in (OUT / "ids.txt").read_text().split()]
    eng = db.engine()
    with eng.connect() as c:
        L = db.listings.c
        leads = {r["id"]: dict(r) for r in c.execute(select(db.listings).where(L.id.in_(ids))).mappings()}
        owners = {}
        for r in c.execute(select(db.owners).where(db.owners.c.listing_id.in_(ids))).mappings():
            owners.setdefault(r["listing_id"], []).append({k: j(v) for k, v in dict(r).items()})
        stored_docs = {}
        for r in c.execute(select(db.sale_docs).where(db.sale_docs.c.listing_id.in_(ids))).mappings():
            stored_docs.setdefault(r["listing_id"], []).append({k: j(v) for k, v in dict(r).items()})
        # Other rows in our own table for the same unit (e.g. a newer listing that is active).
        keys = {(l["norm_address"], l["norm_unit"]) for l in leads.values()}
        same = {}
        for r in c.execute(select(L.id, L.norm_address, L.norm_unit, L.state, L.list_price, L.list_date,
                                  L.off_date, L.list_office, L.feed_status, L.last_seen_active)
                           .where(L.norm_address.in_([k[0] for k in keys]))).mappings():
            same.setdefault((r["norm_address"], r["norm_unit"]), []).append({k: j(v) for k, v in dict(r).items()})

    # One RentCast request for ACTIVE listings over $3M in the same circle, to catch relists.
    active, rc_note = [], ""
    used = pipeline.rentcast_requests_this_month()
    if not rentcast.enabled():
        rc_note = "no RENTCAST_API_KEY"
    elif used + 1 > config.RENTCAST_MONTHLY_CAP:
        rc_note = f"skipped: cap reached ({used}/{config.RENTCAST_MONTHLY_CAP})"
    else:
        counter = {}
        try:
            active = list(rentcast.fetch(3000000, status="Active", budget=1, counter=counter))
            rc_note = f"ok, {len(active)} active listings; requests this month now {used + counter['requests']}"
        except Exception as e:  # noqa: BLE001
            rc_note = f"failed: {e}"
        pipeline._log_requests(counter.get("requests", 0))
    act_idx = {}
    for a in active:
        act_idx.setdefault(normalize.street(a["address"]), []).append(
            {"unit": a["unit"], "norm_unit": normalize.unit(a["unit"]), "price": a["list_price"],
             "list_date": j(a["list_date"]), "office": a["office"], "agent": a["agent"],
             "agent_phone": a["agent_phone"], "type": a["property_type"]})

    out = {"rentcast": rc_note, "run_at": datetime.utcnow().isoformat(), "leads": {}}
    for i, lid in enumerate(ids):
        l = leads.get(lid)
        if not l:
            out["leads"][lid] = {"error": "not in db"}
            continue
        rec = {"address": l["address"], "unit": l["unit"], "borough": l["borough"], "bbl": l["bbl"],
               "state": l["state"], "list_date": j(l["list_date"]), "off_date": j(l["off_date"]),
               "list_price": l["list_price"], "high_price": l["high_price"], "agent": l["list_agent"],
               "office": l["list_office"], "agent_phone": l["list_agent_phone"], "agent_email": l["list_agent_email"],
               "sale_check_note": l["sale_check_note"], "owners_db": owners.get(lid, []),
               "stored_docs": stored_docs.get(lid, []),
               "same_unit_rows": [r for r in same.get((l["norm_address"], l["norm_unit"]), []) if r["id"] != lid],
               "active_same_building": act_idx.get(l["norm_address"], [])}
        try:
            docs = acris.lookup(l["borough"], l["address"], l["unit"], l["bbl"])
            bbl = l["bbl"]
            if not docs:
                g = records.geosearch(l["address"], l["borough"])
                if g and g["bbl"]:
                    bbl = bbl or g["bbl"]
                    docs = acris.lookup(l["borough"], l["address"], l["unit"], g["bbl"])
            since = l["list_date"] or ((l["first_seen"] or db.now()) - timedelta(days=l["days_on_market"] or 0))
            try:
                docs = docs + records.dof_sales(bbl, l["unit"], since - timedelta(days=30))
            except Exception as e:  # noqa: BLE001
                rec["dof_error"] = str(e)
            rec["docs"] = [doc_out(d) for d in docs[:15]]
            doc, parties = acris.current_owner([d for d in docs if d["doc_type"] != "DOF SALE"])
            rec["owner_doc"] = doc_out(doc) if doc else None
            rec["dos"] = {}
            for p in parties:
                if normalize.is_entity(p["name"]):
                    rec["dos"][p["name"]] = records.llc_people(p["name"])
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)
        out["leads"][lid] = rec
        if i % 20 == 0:
            log.info("%s/%s", i, len(ids))
    (OUT / "out.json").write_text(json.dumps(out, indent=1, default=str))
    log.info("done: %s", rc_note)


if __name__ == "__main__":
    main()
