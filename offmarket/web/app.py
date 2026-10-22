"""The website + built-in 6am scheduler."""
import csv
import io
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, desc, func, or_, select

from .. import compliance, config, db, normalize, pipeline, settings
from ..enrich import letter, skiptrace
from ..public import acris
from ..sources import csv_import, reso

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
LEAD_STATUSES = ["New", "Contacted", "Meeting", "Signed", "Dismissed"]
VIEWS = {
    "leads": ("Off market, not sold", ("pulled",)),
    "contract": ("Fell out of contract? (waiting on sale records)", ("contract_off",)),
    "relisted": ("Relisted", ("relisted",)),
    "sold": ("Sold (filtered out)", ("sold",)),
    "active": ("On the market now (watching)", ("active",)),
}
PAGE_SIZE = 50
security = HTTPBasic(auto_error=False)


def auth(request: Request, creds: HTTPBasicCredentials = Depends(security)):
    if not config.DASHBOARD_PASSWORD or request.url.path == "/healthz":
        return
    ok = creds and secrets.compare_digest(creds.username, config.DASHBOARD_USER) and \
        secrets.compare_digest(creds.password, config.DASHBOARD_PASSWORD)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app):
    db.engine()
    if config.ENABLE_SCHEDULER:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(timezone="America/New_York")
        sched.add_job(_run_in_background, "cron", hour=config.DAILY_RUN_HOUR, minute=0, id="daily",
                      misfire_grace_time=6 * 3600, coalesce=True)
        sched.start()
        app.state.scheduler = sched
    yield
    if getattr(app.state, "scheduler", None):
        app.state.scheduler.shutdown(wait=False)


app = FastAPI(title="Off-Market Luxury Leads", dependencies=[Depends(auth)], lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def money(v):
    if not v:
        return "—"
    return f"${v / 1e6:.2f}M".replace(".00M", "M") if v >= 1e6 else f"${v:,.0f}"


templates.env.filters.update(money=money, phone=normalize.fmt_phone,
                             d=lambda v, f="%b %d, %Y": v.strftime(f) if v else "—")
templates.env.globals.update(LEAD_STATUSES=LEAD_STATUSES, VIEWS=VIEWS, doc_url=acris.doc_url, config=config,
                             DEMO=os.getenv("DEMO") == "1")

_run_lock = threading.Lock()
_run_state = {"running": False}


def _run_in_background():
    if not _run_lock.acquire(blocking=False):
        return False

    def work():
        _run_state["running"] = True
        try:
            pipeline.run_daily()
        except Exception:
            log.exception("daily run failed")
        finally:
            _run_state["running"] = False
            _run_lock.release()
    threading.Thread(target=work, daemon=True).start()
    return True


def _filters(request):
    p = request.query_params
    f = {k: p.get(k, "") for k in ("view", "q", "area", "type", "tier", "status", "min", "max", "days", "contact",
                                   "sort")}
    f["view"] = f["view"] if f["view"] in VIEWS else "leads"
    return f


def _query(f):
    L = db.listings.c
    conds = [L.state.in_(VIEWS[f["view"]][1])]
    if f["status"]:
        conds.append(L.lead_status == f["status"])
    elif f["view"] == "leads":
        conds.append(L.lead_status != "Dismissed")
    if f["area"]:
        conds.append(or_(L.borough == f["area"], L.neighborhood == f["area"]))
    if f["type"]:
        conds.append(L.property_type == f["type"])
    if f["tier"]:
        conds.append(L.tier == f["tier"])
    for key, op in (("min", "ge"), ("max", "le")):
        v = normalize.price(f[key])
        if v:
            v = v * 1_000_000 if v < 1000 else v
            conds.append(L.high_price >= v if op == "ge" else L.high_price <= v)
    if f["days"].isdigit():
        conds.append(L.off_date >= db.now() - timedelta(days=int(f["days"])))
    if f["contact"] == "phone":
        conds.append(L.id.in_(select(db.contacts.c.listing_id).where(and_(
            db.contacts.c.kind == "phone", db.contacts.c.dnc.is_(False), db.contacts.c.tcpa.is_(False),
            db.contacts.c.suppressed.is_(False)))))
    elif f["contact"] == "any":
        conds.append(or_(L.id.in_(select(db.contacts.c.listing_id)),
                         L.id.in_(select(db.owners.c.listing_id).where(db.owners.c.mailing_address != ""))))
    if f["q"]:
        like = f"%{f['q'].upper()}%"
        conds.append(or_(func.upper(L.address).like(like), func.upper(L.neighborhood).like(like),
                         func.upper(L.list_office).like(like), func.upper(L.list_agent).like(like),
                         L.id.in_(select(db.owners.c.listing_id).where(func.upper(db.owners.c.name).like(like)))))
    order = {
        "price": [desc(L.high_price)], "off": [desc(L.off_date)], "dom": [desc(L.days_on_market)],
    }.get(f["sort"], [desc(L.score), desc(L.off_date)])
    return select(db.listings).where(and_(*conds)).order_by(*order)


@app.get("/", response_class=HTMLResponse)
def leads(request: Request, page: int = 1):
    f = _filters(request)
    s = settings.get_all()
    L = db.listings.c
    with db.engine().connect() as conn:
        q = _query(f)
        total = conn.execute(select(func.count()).select_from(q.subquery())).scalar()
        rows = conn.execute(q.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).mappings().all()
        ids = [r["id"] for r in rows]
        owners, contact_flags = {}, {}
        if ids:
            for o in conn.execute(select(db.owners).where(db.owners.c.listing_id.in_(ids))).mappings():
                owners.setdefault(o["listing_id"], []).append(o)
            for c in conn.execute(select(db.contacts).where(db.contacts.c.listing_id.in_(ids))).mappings():
                fl = contact_flags.setdefault(c["listing_id"], {"phone": 0, "email": 0, "dnc": 0})
                if c["kind"] == "phone" and not (c["dnc"] or c["tcpa"] or c["suppressed"]):
                    fl["phone"] += 1
                elif c["kind"] == "phone":
                    fl["dnc"] += 1
                elif not c["suppressed"]:
                    fl["email"] += 1
        counts = dict(conn.execute(select(L.state, func.count()).group_by(L.state)).all())
        open_leads = and_(L.state == "pulled", L.lead_status != "Dismissed")
        tiers = dict(conn.execute(select(L.tier, func.count()).where(open_leads).group_by(L.tier)).all())
        week = conn.execute(select(func.count()).where(and_(open_leads, L.off_date >= db.now() - timedelta(days=7)))
                            ).scalar()
        last_run = conn.execute(select(func.max(db.runs.c.finished_at)).where(db.runs.c.step == "sold_check")).scalar()
        areas = ["Manhattan"] + sorted({r[0] for r in conn.execute(select(L.neighborhood).where(
            L.borough == "Brooklyn").distinct()) if r[0]})
    return templates.TemplateResponse(request, "leads.html", {
        "rows": rows, "owners": owners, "cflags": contact_flags, "f": f, "total": total, "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)), "counts": counts, "tiers": tiers, "week": week,
        "last_run": last_run, "running": _run_state["running"], "areas": areas, "types": s["property_types"],
        "qs": "&".join(f"{k}={v}" for k, v in f.items() if v and k != "page"),
        "feed_on": reso.enabled(), "has_any": sum(counts.values()) > 0,
    })


def _listing_bundle(conn, lid):
    l = conn.execute(select(db.listings).where(db.listings.c.id == lid)).mappings().first()
    if not l:
        raise HTTPException(404)
    return {
        "l": l,
        "events": conn.execute(select(db.events).where(db.events.c.listing_id == lid)
                               .order_by(db.events.c.at, db.events.c.id)).mappings().all(),
        "owners": conn.execute(select(db.owners).where(db.owners.c.listing_id == lid)).mappings().all(),
        "contacts": conn.execute(select(db.contacts).where(db.contacts.c.listing_id == lid)
                                 .order_by(db.contacts.c.kind.desc(), db.contacts.c.dnc)).mappings().all(),
        "docs": conn.execute(select(db.sale_docs).where(db.sale_docs.c.listing_id == lid)
                             .order_by(desc(db.sale_docs.c.doc_date))).mappings().all(),
        "others": conn.execute(select(db.listings).where(and_(
            db.listings.c.norm_address == l["norm_address"], db.listings.c.norm_unit == l["norm_unit"],
            db.listings.c.id != lid)).order_by(desc(db.listings.c.first_seen))).mappings().all(),
    }


@app.get("/listing/{lid}", response_class=HTMLResponse)
def listing(request: Request, lid: int):
    with db.engine().connect() as conn:
        b = _listing_bundle(conn, lid)
        sup = compliance.suppressed_set(conn)
    b["suppressed"] = compliance.listing_suppressed(sup, b["l"], [o["name"] for o in b["owners"]])
    b.update(skiptrace_on=skiptrace.enabled(), msg=request.query_params.get("msg", ""))
    return templates.TemplateResponse(request, "listing.html", b)


@app.post("/listing/{lid}")
def update_listing(lid: int, lead_status: str = Form(...), notes: str = Form("")):
    if lead_status not in LEAD_STATUSES:
        raise HTTPException(400)
    with db.engine().begin() as conn:
        conn.execute(db.listings.update().where(db.listings.c.id == lid).values(lead_status=lead_status, notes=notes,
                                                                                 updated_at=db.now()))
    return RedirectResponse(f"/listing/{lid}?msg=Saved", status_code=303)


@app.post("/listing/{lid}/recheck")
def recheck(lid: int):
    s = settings.get_all()
    try:
        with db.engine().begin() as conn:
            l = conn.execute(select(db.listings).where(db.listings.c.id == lid)).mappings().first()
            sold = pipeline.check_one(conn, l, s)
            pipeline.rescore(conn)
        msg = "A recorded sale was found: moved to Sold" if sold else "Checked public records: no sale recorded"
    except Exception as e:  # noqa: BLE001
        msg = f"Public records lookup failed: {e}"
    return RedirectResponse(f"/listing/{lid}?msg={msg}", status_code=303)


@app.post("/listing/{lid}/skiptrace")
def run_skiptrace(lid: int):
    if not skiptrace.enabled():
        return RedirectResponse(f"/listing/{lid}?msg=Add a BatchData API key to turn on contact lookup",
                                status_code=303)
    try:
        with db.engine().begin() as conn:
            n = pipeline.skiptrace_one(conn, lid)
            pipeline.rescore(conn)
        msg = f"Found {n} phone numbers and emails"
    except Exception as e:  # noqa: BLE001
        msg = f"Contact lookup failed: {e}"
    return RedirectResponse(f"/listing/{lid}?msg={msg}", status_code=303)


@app.get("/listing/{lid}/letter", response_class=HTMLResponse)
def outreach_letter(request: Request, lid: int):
    with db.engine().connect() as conn:
        b = _listing_bundle(conn, lid)
    text, by_claude = letter.draft(b["l"], b["owners"], b["events"])
    return templates.TemplateResponse(request, "letter.html", {**b, "text": text, "by_claude": by_claude})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with db.engine().connect() as conn:
        sup = conn.execute(select(db.suppression).order_by(desc(db.suppression.c.created_at))).mappings().all()
    s = settings.get_all()
    hoods = "\n".join(f"{k}: {', '.join(v)}" for k, v in s["brooklyn_neighborhoods"].items())
    return templates.TemplateResponse(request, "settings.html", {
        "s": s, "hoods": hoods, "sup": sup, "msg": request.query_params.get("msg", ""),
        "all_types": ["Condo", "Co-op", "Townhouse", "Single family", "Multi-family"],
        "cd_note": compliance.CEASE_DESIST_NOTE, "skiptrace_on": skiptrace.enabled(),
    })


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    hoods = {}
    for line in str(form.get("brooklyn_neighborhoods", "")).splitlines():
        if ":" in line:
            name, zips = line.split(":", 1)
            hoods[name.strip()] = [z.strip() for z in zips.replace(";", ",").split(",") if z.strip()]
        elif line.strip():
            hoods[line.strip()] = []
    try:
        values = {
            "min_price": normalize.price(form.get("min_price")) or settings.DEFAULTS["min_price"],
            "use_high_price": form.get("use_high_price") == "on",
            "manhattan_all": form.get("manhattan_all") == "on",
            "brooklyn_neighborhoods": hoods,
            "property_types": form.getlist("property_types"),
            "sale_min_ratio": float(form.get("sale_min_ratio") or 0.5),
            "sale_recheck_days": int(form.get("sale_recheck_days") or 365),
            "contract_wait_days": int(form.get("contract_wait_days") or 120),
            "missing_days": int(form.get("missing_days") or 2),
            "auto_skiptrace_tiers": form.getlist("auto_skiptrace_tiers"),
        }
    except ValueError:
        return RedirectResponse("/settings?msg=Please enter numbers in the number fields", status_code=303)
    settings.save(values)
    return RedirectResponse("/settings?msg=Settings saved. They apply from the next refresh.", status_code=303)


@app.post("/suppression")
def add_suppression(kind: str = Form(...), value: str = Form(...), reason: str = Form("")):
    if kind not in ("phone", "email", "name", "address"):
        raise HTTPException(400)
    with db.engine().begin() as conn:
        for v in [x.strip() for x in value.splitlines() if x.strip()]:
            conn.execute(db.suppression.insert().values(kind=kind, value=v, norm=normalize.suppression_norm(kind, v),
                                                        reason=reason or "Do not contact", created_at=db.now()))
        _reflag_contacts(conn)
    return RedirectResponse("/settings?msg=Added to the do-not-contact list#dnc", status_code=303)


@app.post("/suppression/{sid}/delete")
def del_suppression(sid: int):
    with db.engine().begin() as conn:
        conn.execute(db.suppression.delete().where(db.suppression.c.id == sid))
        _reflag_contacts(conn)
    return RedirectResponse("/settings?msg=Removed#dnc", status_code=303)


def _reflag_contacts(conn):
    sup = compliance.suppressed_set(conn)
    for c in conn.execute(select(db.contacts)).mappings().all():
        flag = bool(compliance.check(sup, c["kind"], c["value"]) or compliance.check(sup, "name", c["person"]))
        if flag != bool(c["suppressed"]):
            conn.execute(db.contacts.update().where(db.contacts.c.id == c["id"]).values(suppressed=flag))


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    with db.engine().connect() as conn:
        history = conn.execute(select(db.runs).order_by(desc(db.runs.c.started_at)).limit(30)).mappings().all()
        by_source = dict(conn.execute(select(db.listings.c.source, func.count()).group_by(db.listings.c.source)).all())
    return templates.TemplateResponse(request, "sources.html", {
        "history": history, "by_source": by_source, "feed_on": reso.enabled(), "feed_label": reso.label,
        "skiptrace_on": skiptrace.enabled(), "claude_on": bool(config.ANTHROPIC_API_KEY),
        "running": _run_state["running"], "msg": request.query_params.get("msg", ""),
    })


@app.post("/run")
def run_now():
    started = _run_in_background()
    msg = "Refresh started. It takes a few minutes; reload this page to see results." if started else \
        "A refresh is already running."
    return RedirectResponse(f"/sources?msg={msg}", status_code=303)


@app.post("/import")
async def import_csv(file: UploadFile = File(...), full_snapshot: str = Form("")):
    data = await file.read()
    try:
        rows, has_status = csv_import.parse(data, file.filename or "upload.csv")
    except ValueError as e:
        return RedirectResponse(f"/sources?msg={e}", status_code=303)
    st = pipeline.import_rows(rows, full_snapshot == "on", file.filename)
    msg = (f"Imported {file.filename}: {st['seen']} listings in your areas and price range "
           f"({st['new']} new, {st['pulled'] + st.get('missing', 0)} off the market, {st['sold']} sold, "
           f"{st['skipped']} outside your criteria). Public-records checks run with the next refresh.")
    return RedirectResponse(f"/sources?msg={msg}", status_code=303)


@app.get("/export.csv")
def export(request: Request):
    f = _filters(request)
    with db.engine().connect() as conn:
        rows = conn.execute(_query(f)).mappings().all()
        ids = [r["id"] for r in rows]
        owners, contacts = {}, {}
        for o in conn.execute(select(db.owners).where(db.owners.c.listing_id.in_(ids))).mappings():
            owners.setdefault(o["listing_id"], []).append(o)
        for c in conn.execute(select(db.contacts).where(db.contacts.c.listing_id.in_(ids))).mappings():
            contacts.setdefault(c["listing_id"], []).append(c)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["score", "tier", "address", "unit", "neighborhood", "borough", "zip", "type", "beds", "baths",
                "highest_ask", "last_ask", "off_market_date", "reason", "days_on_market", "list_office",
                "owner", "owner_mailing_address", "callable_phones", "do_not_call_phones", "emails", "lead_status",
                "notes", "link"])
    for r in rows:
        os_ = owners.get(r["id"], [])
        cs = contacts.get(r["id"], [])
        w.writerow([
            r["score"], r["tier"], r["address"], r["unit"], r["neighborhood"], r["borough"], r["zip"],
            r["property_type"], r["beds"], r["baths"], r["high_price"], r["list_price"],
            r["off_date"].date() if r["off_date"] else "", r["off_reason"], r["days_on_market"], r["list_office"],
            "; ".join(o["name"] for o in os_), "; ".join(sorted({o["mailing_address"] for o in os_ if o["mailing_address"]})),
            "; ".join(normalize.fmt_phone(c["value"]) for c in cs if c["kind"] == "phone"
                      and not (c["dnc"] or c["tcpa"] or c["suppressed"])),
            "; ".join(normalize.fmt_phone(c["value"]) for c in cs if c["kind"] == "phone"
                      and (c["dnc"] or c["tcpa"] or c["suppressed"])),
            "; ".join(c["value"] for c in cs if c["kind"] == "email" and not c["suppressed"]),
            r["lead_status"], r["notes"], str(request.base_url).rstrip("/") + f"/listing/{r['id']}",
        ])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=off-market-leads.csv"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "running": _run_state["running"]}
