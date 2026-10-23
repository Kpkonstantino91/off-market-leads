"""The daily job: pull listings -> spot the ones that came off the market -> drop the ones that sold
-> find the owner -> (optionally) skip trace -> score."""
import logging
import traceback
from datetime import timedelta

from sqlalchemy import and_, or_, select

from . import compliance, config, db, normalize, scoring, settings
from .enrich import skiptrace
from .public import acris, records
from .sources import base, rentcast, reso

log = logging.getLogger(__name__)
OPEN_STATES = ("pulled", "contract_off")


# ---------------------------------------------------------------- ingest

def wanted(s, row):
    if row["property_type"] == "Rental":
        return False
    if row["property_type"] not in s["property_types"] and row["property_type"] != "Other":
        return False
    return settings.in_area(s, row["borough"], row["neighborhood"], row["zip"])


def ingest(conn, rows, s, today=None):
    """Upsert listings from a source and apply status changes. Returns counts."""
    today = today or db.now()
    L = db.listings.c
    stats = {"seen": 0, "new": 0, "pulled": 0, "sold": 0, "relisted": 0, "skipped": 0}
    for row in rows:
        if not row.get("listing_key") or not wanted(s, row):
            stats["skipped"] += 1
            continue
        cur = conn.execute(select(db.listings).where(and_(L.source == row["source"],
                                                          L.listing_key == row["listing_key"]))).mappings().first()
        high = max(x for x in [row["list_price"] or 0, row["original_price"] or 0, (cur or {}).get("high_price") or 0])
        if (high if s["use_high_price"] else (row["list_price"] or 0)) < s["min_price"]:
            stats["skipped"] += 1
            continue
        stats["seen"] += 1
        vals = {
            "listing_url": row["url"], "address": row["address"], "unit": row["unit"],
            "norm_address": normalize.street(row["address"]), "norm_unit": normalize.unit(row["unit"]),
            "borough": row["borough"], "neighborhood": settings.area_label(s, row["borough"], row["neighborhood"],
                                                                          row["zip"]),
            "zip": row["zip"], "property_type": row["property_type"], "beds": row["beds"], "baths": row["baths"],
            "sqft": row["sqft"], "list_agent": row["agent"], "list_office": row["office"],
            "list_agent_phone": row["agent_phone"], "list_agent_email": row["agent_email"],
            "remarks": row["remarks"], "photo_url": row["photo_url"], "lat": row["lat"], "lon": row["lon"],
            "feed_status": row["status"], "high_price": high, "updated_at": today, "raw": row["raw"],
        }
        if row["bbl"]:
            vals["bbl"] = row["bbl"]
        if row["list_price"]:
            vals["list_price"] = row["list_price"]
        if row["list_date"]:
            vals["list_date"] = row["list_date"]
        events = []
        if cur is None:
            vals.update(source=row["source"], listing_key=row["listing_key"], first_seen=today, state="active",
                        original_price=row["original_price"] or row["list_price"], price_cuts=0, lead_status="New",
                        notes="")
            if row["original_price"] and row["list_price"] and row["list_price"] < row["original_price"]:
                vals["price_cuts"] = 1
            vals["prior_pulls"] = _prior_pulls(conn, vals, row, stats, today)
            lid = conn.execute(db.listings.insert().values(**vals)).inserted_primary_key[0]
            stats["new"] += 1
            events.append(("listed", f"Listed at ${(row['list_price'] or 0):,}" + (
                f" by {row['office']}" if row["office"] else ""), row["list_price"], row["list_date"] or today))
            cur = {"id": lid, "state": "active", "was_in_contract": False, "list_price": row["list_price"],
                   "price_cuts": vals["price_cuts"], "off_date": None}
        else:
            lid = cur["id"]
            if row["list_price"] and cur["list_price"] and row["list_price"] != cur["list_price"]:
                direction = "cut" if row["list_price"] < cur["list_price"] else "raised"
                events.append(("price", f"Price {direction} ${cur['list_price']:,} → ${row['list_price']:,}",
                               row["list_price"], row["status_date"] or today))
                if direction == "cut":
                    vals["price_cuts"] = (cur["price_cuts"] or 0) + 1
            conn.execute(db.listings.update().where(L.id == lid).values(**vals))

        _apply_status(conn, lid, cur, row, today, events, stats)
        for kind, detail, price, at in events:
            conn.execute(db.events.insert().values(listing_id=lid, at=at, kind=kind, detail=detail, price=price))
    return stats


def _prior_pulls(conn, vals, row, stats, today):
    """A new listing of a unit we saw pulled before: mark the old one relisted and count the failures."""
    L = db.listings.c
    olds = conn.execute(select(L.id, L.state, L.prior_pulls).where(and_(
        L.norm_address == vals["norm_address"], L.norm_unit == vals["norm_unit"], L.borough == vals["borough"]))
    ).mappings().all()
    prior = 0
    for o in olds:
        if o["state"] in OPEN_STATES and row["status"] in base.ON_MARKET:
            conn.execute(db.listings.update().where(L.id == o["id"]).values(state="relisted", updated_at=today))
            conn.execute(db.events.insert().values(listing_id=o["id"], at=today, kind="relisted",
                                                   detail=f"Back on the market ({row['office'] or 'new listing'})"))
            stats["relisted"] += 1
        if o["state"] in OPEN_STATES or o["state"] == "relisted":
            prior = max(prior, (o["prior_pulls"] or 0) + 1)
    return prior


def _apply_status(conn, lid, cur, row, today, events, stats):
    L = db.listings.c
    st = row["status"]
    upd = {}
    if st in base.ON_MARKET or (st == base.UNKNOWN and cur["state"] == "active"):
        upd["last_seen_active"] = today
        if st == base.PENDING and not cur["was_in_contract"]:
            upd["was_in_contract"] = True
            events.append(("status", "Went into contract", None, row["status_date"] or today))
        if cur["state"] in OPEN_STATES:
            upd.update(state="active", off_reason=None, off_date=None)
            events.append(("relisted", "Back on the market", None, today))
            stats["relisted"] += 1
    elif st == base.CLOSED:
        if cur["state"] != "sold":
            upd.update(state="sold", sold_price=row["close_price"], sold_date=row["close_date"] or row["status_date"],
                       sold_source="Listing feed (closed)")
            events.append(("sold", f"Closed{' at $' + format(row['close_price'], ',') if row['close_price'] else ''}",
                           row["close_price"], row["close_date"] or today))
            stats["sold"] += 1
    elif st in base.PULLED:
        if cur["state"] in ("active",) or (cur["state"] in OPEN_STATES and not cur.get("off_date")):
            was_contract = cur["was_in_contract"] or upd.get("was_in_contract")
            off = row["status_date"] or today
            upd.update(state="contract_off" if was_contract else "pulled", off_reason=st, off_date=off)
            events.append(("off", f"{st}" + (f" by {row['office']}" if row["office"] else ""), None, off))
            stats["pulled"] += 1
    if row["days_on_market"]:
        upd["days_on_market"] = int(row["days_on_market"])
    if upd:
        conn.execute(db.listings.update().where(L.id == lid).values(**upd))
    if not row["days_on_market"]:
        _fill_dom(conn, lid, today)


def _fill_dom(conn, lid, today):
    """Days on market from the list date when the feed doesn't say."""
    l = conn.execute(select(db.listings).where(db.listings.c.id == lid)).mappings().first()
    if not l["list_date"]:
        return
    end = l["off_date"] or today
    conn.execute(db.listings.update().where(db.listings.c.id == lid).values(
        days_on_market=max(0, (end - l["list_date"]).days)))


def mark_missing(conn, source, seen_keys, s, today=None, fetch_one=None):
    """Active listings from a full snapshot that weren't in today's snapshot.

    Asks the feed for the listing's current status when it can; otherwise, once a listing has been
    missing for `missing_days`, it's treated as having left the market ("Disappeared")."""
    today = today or db.now()
    L = db.listings.c
    gone = conn.execute(select(db.listings).where(and_(L.source == source, L.state == "active"))).mappings().all()
    n = 0
    for l in gone:
        if l["listing_key"] in seen_keys:
            continue
        if fetch_one:
            try:
                row = fetch_one(l["listing_key"])
            except Exception as e:  # noqa: BLE001
                log.warning("could not fetch %s: %s", l["listing_key"], e)
                row = None
            if row and row["status"] != base.UNKNOWN and row["status"] not in base.ON_MARKET:
                ingest(conn, [row], s, today)
                n += 1
                continue
        last = l["last_seen_active"] or l["first_seen"]
        if last and (today - last).days >= s["missing_days"]:
            off = last + timedelta(days=1)
            conn.execute(db.listings.update().where(L.id == l["id"]).values(
                state="contract_off" if l["was_in_contract"] else "pulled", off_reason="Disappeared",
                off_date=off, updated_at=today))
            conn.execute(db.events.insert().values(listing_id=l["id"], at=off, kind="off",
                                                   detail="Disappeared from the active listings"))
            _fill_dom(conn, l["id"], today)
            n += 1
    return n


# ---------------------------------------------------------------- sold check + owner

def check_one(conn, l, s, today=None):
    """Look the unit up in ACRIS (+ DOF sales): record docs, set owner, and mark sold if a sale recorded."""
    today = today or db.now()
    L = db.listings.c
    docs = acris.lookup(l["borough"], l["address"], l["unit"], l["bbl"])
    bbl = l["bbl"]
    lat, lon = l["lat"], l["lon"]
    if not docs or not lat:
        g = records.geosearch(l["address"], l["borough"])
        if g:
            bbl = bbl or g["bbl"]
            lat, lon = lat or g["lat"], lon or g["lon"]
            if not docs and bbl:
                docs = acris.lookup(l["borough"], l["address"], l["unit"], bbl)
    if docs and not bbl:
        bbl = docs[0].get("bbl")
    since = l["list_date"] or (l["first_seen"] - timedelta(days=l["days_on_market"] or 0))
    try:
        docs = docs + records.dof_sales(bbl, l["unit"], since)
    except Exception as e:  # noqa: BLE001 - second check is best-effort
        log.warning("DOF sales lookup failed: %s", e)
    ref_price = l["list_price"] or l["high_price"] or 0
    min_amount = int(ref_price * s["sale_min_ratio"])
    sale = None
    for d in docs:
        counts = acris.is_sale(d, since, min_amount) or (d["doc_type"] == "DOF SALE" and d["doc_date"] >= since
                                                         and d["amount"] >= min_amount)
        conn.execute(db.sale_docs.delete().where(and_(db.sale_docs.c.listing_id == l["id"],
                                                      db.sale_docs.c.doc_id == d["doc_id"])))
        conn.execute(db.sale_docs.insert().values(
            listing_id=l["id"], doc_id=d["doc_id"], doc_type=d["doc_type"], doc_date=d["doc_date"],
            recorded=d["recorded"], amount=d["amount"], unit=d["unit"],
            parties_from="; ".join(p["name"] for p in d["from"]), parties_to="; ".join(p["name"] for p in d["to"]),
            counts_as_sale=bool(counts), source="DOF rolling sales" if d["doc_type"] == "DOF SALE" else "ACRIS"))
        if counts and (sale is None or d["doc_date"] < sale["doc_date"]):
            sale = d

    upd = {"bbl": bbl, "lat": lat, "lon": lon, "sale_checked_at": today, "owner_checked_at": today}
    if sale:
        upd.update(state="sold", sold_price=sale["amount"], sold_date=sale["doc_date"],
                   sold_source="ACRIS" if sale["doc_type"] != "DOF SALE" else "DOF rolling sales",
                   sale_check_note=f"Sale recorded {sale['doc_date']:%b %d, %Y} for ${sale['amount']:,}")
        conn.execute(db.events.insert().values(listing_id=l["id"], at=sale["doc_date"], kind="sold",
                                               detail=f"Sale recorded ({sale['doc_type']}) ${sale['amount']:,}",
                                               price=sale["amount"]))
    else:
        upd["sale_check_note"] = (f"No sale recorded since {since:%b %d, %Y} ({len(docs)} documents checked). "
                                  "ACRIS usually posts deeds 1–6 weeks after closing; re-checked daily.")
        if l["state"] == "contract_off" and l["off_date"] and (today - l["off_date"]).days >= s["contract_wait_days"]:
            upd["state"] = "pulled"
            conn.execute(db.events.insert().values(
                listing_id=l["id"], at=today, kind="note",
                detail=f"No sale recorded {s['contract_wait_days']} days after leaving the market: contract fell through"))
    conn.execute(db.listings.update().where(L.id == l["id"]).values(**upd))
    _set_owner(conn, l["id"], docs)
    return bool(sale)


def _set_owner(conn, lid, docs):
    doc, parties = acris.current_owner([d for d in docs if d["doc_type"] != "DOF SALE"])
    if not doc:
        return
    conn.execute(db.owners.delete().where(and_(db.owners.c.listing_id == lid, db.owners.c.source == "ACRIS")))
    for p in parties:
        ent = normalize.is_entity(p["name"])
        people = records.llc_people(p["name"]) if ent else None
        conn.execute(db.owners.insert().values(
            listing_id=lid, name=normalize.person_name(p["name"]), is_entity=ent, mailing_address=p["address"],
            acquired_date=doc["doc_date"], acquired_price=doc["amount"] or None, doc_id=doc["doc_id"],
            doc_type=doc["doc_type"], entity_people=people, source="ACRIS"))


def check_sales(conn, s, today=None, limit=400):
    today = today or db.now()
    L = db.listings.c
    stale = today - timedelta(hours=20)
    q = select(db.listings).where(and_(
        L.state.in_(OPEN_STATES),
        or_(L.off_date.is_(None), L.off_date >= today - timedelta(days=s["sale_recheck_days"])),
        or_(L.sale_checked_at.is_(None), L.sale_checked_at < stale),
    )).order_by(L.sale_checked_at.is_not(None), L.off_date.desc()).limit(limit)
    stats = {"checked": 0, "sold": 0, "errors": 0}
    for l in conn.execute(q).mappings().all():
        try:
            stats["sold"] += check_one(conn, l, s, today)
            stats["checked"] += 1
        except Exception as e:  # noqa: BLE001 - one bad lookup shouldn't stop the run
            stats["errors"] += 1
            log.warning("sale check failed for %s %s: %s", l["address"], l["unit"], e)
    return stats


# ---------------------------------------------------------------- contacts

def skiptrace_one(conn, lid, today=None):
    today = today or db.now()
    l = conn.execute(select(db.listings).where(db.listings.c.id == lid)).mappings().first()
    found = skiptrace.lookup(l["address"], l["unit"], l["borough"], l["zip"])
    sup = compliance.suppressed_set(conn)
    conn.execute(db.contacts.delete().where(and_(db.contacts.c.listing_id == lid,
                                                 db.contacts.c.source == skiptrace.name)))
    for c in found:
        conn.execute(db.contacts.insert().values(
            listing_id=lid, person=c["person"], kind=c["kind"], value=c["value"], phone_type=c["phone_type"],
            dnc=c["dnc"], tcpa=c["tcpa"], suppressed=bool(compliance.check(sup, c["kind"], c["value"])
                                                          or compliance.check(sup, "name", c["person"])),
            source=skiptrace.name, created_at=today))
    conn.execute(db.listings.update().where(db.listings.c.id == lid).values(skiptraced_at=today))
    return len(found)


def auto_skiptrace(conn, s):
    if not skiptrace.enabled() or not s["auto_skiptrace_tiers"]:
        return {"skipped": "off"}
    L = db.listings.c
    ids = [r.id for r in conn.execute(select(L.id).where(and_(
        L.state == "pulled", L.skiptraced_at.is_(None), L.tier.in_(s["auto_skiptrace_tiers"]),
        L.lead_status != "Dismissed")).limit(200))]
    n = 0
    for lid in ids:
        try:
            skiptrace_one(conn, lid)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("skip trace failed for listing %s: %s", lid, e)
    return {"traced": n}


# ---------------------------------------------------------------- scoring

def rescore(conn, today=None):
    L = db.listings.c
    rows = conn.execute(select(db.listings).where(L.state.in_(OPEN_STATES + ("relisted", "sold")))).mappings().all()
    for l in rows:
        owners = conn.execute(select(db.owners).where(db.owners.c.listing_id == l["id"])).mappings().all()
        contacts = conn.execute(select(db.contacts).where(db.contacts.c.listing_id == l["id"])).mappings().all()
        total, tier, why = scoring.score(dict(l), owners, contacts, today)
        conn.execute(db.listings.update().where(L.id == l["id"]).values(score=total, tier=tier, score_reasons=why))
    return len(rows)


# ---------------------------------------------------------------- daily run

def _step(name, fn):
    started = db.now()
    try:
        out = fn()
        status, msg = ("skipped", out.get("skipped")) if isinstance(out, dict) and out.get("skipped") else ("ok", "")
    except Exception as e:  # noqa: BLE001
        log.exception("step %s failed", name)
        out, status, msg = None, "error", f"{e}\n{traceback.format_exc()[-1500:]}"
    with db.engine().begin() as conn:
        conn.execute(db.runs.insert().values(step=name, started_at=started, finished_at=db.now(), status=status,
                                             message=msg, stats=out if isinstance(out, dict) else {"n": out}))
    return out


def _last_ok(conn, step):
    from sqlalchemy import func
    return conn.execute(select(func.max(db.runs.c.started_at)).where(and_(db.runs.c.step == step,
                                                                         db.runs.c.status == "ok"))).scalar()


def run_feed():
    if not reso.enabled():
        return {"skipped": "no feed credentials (RESO_* settings)"}
    s = settings.get_all()
    with db.engine().begin() as conn:
        since = _last_ok(conn, "feed") or (db.now() - timedelta(days=90))
    floor = int(s["min_price"] * (0.6 if s["use_high_price"] else 1))
    active = list(reso.fetch_active(floor))
    changed = list(reso.fetch_changed(floor, since - timedelta(hours=2)))
    with db.engine().begin() as conn:
        st = ingest(conn, active, s)
        st2 = ingest(conn, changed, s)
        st["pulled"] += st2["pulled"]
        st["sold"] += st2["sold"]
        st["missing"] = mark_missing(conn, reso.name, {r["listing_key"] for r in active} |
                                     {r["listing_key"] for r in changed if r["status"] in base.ON_MARKET},
                                     s, fetch_one=reso.fetch_one)
    st["fetched_active"], st["fetched_changed"] = len(active), len(changed)
    return st


def run_rentcast():
    if not rentcast.enabled():
        return {"skipped": "no RENTCAST_API_KEY"}
    used = rentcast_requests_this_month()
    left = config.RENTCAST_MONTHLY_CAP - used
    if left <= 0:
        return {"skipped": f"monthly RentCast limit reached ({used} of {config.RENTCAST_MONTHLY_CAP}); resumes next month"}
    s = settings.get_all()
    floor = int(s["min_price"] * (0.6 if s["use_high_price"] else 1))
    counter = {}
    try:
        rows = list(rentcast.fetch(floor, budget=left, counter=counter))
    except Exception as e:
        _log_requests(counter.get("requests", 0))
        raise RuntimeError(f"RentCast request failed: {e}") from e
    with db.engine().begin() as conn:
        st = ingest(conn, rows, s)
    st["fetched"] = len(rows)
    st["requests"] = counter.get("requests", 0)
    st["requests_this_month"] = used + st["requests"]
    return st


def _log_requests(n):
    """Record requests made by a run that then failed, so they still count against the monthly cap."""
    if n:
        with db.engine().begin() as conn:
            conn.execute(db.runs.insert().values(step="rentcast_usage", started_at=db.now(), finished_at=db.now(),
                                                 status="ok", stats={"requests": n}))


def rentcast_requests_this_month():
    start = db.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with db.engine().connect() as conn:
        rows = conn.execute(select(db.runs.c.stats).where(and_(
            db.runs.c.step.in_(("rentcast", "rentcast_usage")), db.runs.c.started_at >= start))).all()
    return sum(int((r.stats or {}).get("requests", 0)) for r in rows)


def import_rows(rows, full_snapshot, filename):
    s = settings.get_all()
    with db.engine().begin() as conn:
        st = ingest(conn, rows, s)
        if full_snapshot:
            st["missing"] = mark_missing(conn, "csv", {r["listing_key"] for r in rows}, s)
    with db.engine().begin() as conn:
        conn.execute(db.runs.insert().values(step="import", started_at=db.now(), finished_at=db.now(), status="ok",
                                             message=filename, stats=st))
    return st


def run_daily():
    out = {"feed": _step("feed", run_feed), "rentcast": _step("rentcast", run_rentcast)}
    s = settings.get_all()
    out["sales"] = _step("sold_check", lambda: _with_conn(lambda c: check_sales(c, s)))
    out["score"] = _step("score", lambda: _with_conn(lambda c: {"scored": rescore(c)}))
    out["skiptrace"] = _step("skiptrace", lambda: _with_conn(lambda c: auto_skiptrace(c, s)))
    if out["skiptrace"] and out["skiptrace"].get("traced"):
        _step("score", lambda: _with_conn(lambda c: {"scored": rescore(c)}))
    return out


def _with_conn(fn):
    with db.engine().begin() as conn:
        return fn(conn)
