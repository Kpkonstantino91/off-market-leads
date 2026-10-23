"""Morning email: the leads that are new since the last email, best first.

Sends through any SMTP account (e.g. a Google Workspace / Gmail mailbox with an app password).
Off until SMTP_USER, SMTP_PASSWORD and DIGEST_TO are set.
"""
import html
import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy import and_, select

from . import config, db

log = logging.getLogger(__name__)
MAX_ROWS = 25


def enabled():
    return bool(config.SMTP_USER and config.SMTP_PASSWORD and config.DIGEST_TO)


def _money(v):
    return f"${v / 1e6:.2f}M".replace(".00M", "M") if v else "—"


def new_leads(conn):
    """Open leads never included in an email, best first."""
    L = db.listings.c
    sent = select(db.digest_sent.c.listing_id)
    return conn.execute(select(db.listings).where(and_(
        L.state == "pulled", L.lead_status != "Dismissed", L.id.not_in(sent),
        L.sale_checked_at.is_not(None),        # only after the sold check has cleared it
    )).order_by(L.score.desc(), L.off_date.desc())).mappings().all()


def build(conn, leads):
    ids = [l["id"] for l in leads]
    owners = {}
    if ids:
        for o in conn.execute(select(db.owners).where(db.owners.c.listing_id.in_(ids))).mappings():
            owners.setdefault(o["listing_id"], []).append(o)
    site = config.SITE_URL.rstrip("/")
    hot = sum(1 for l in leads if l["tier"] == "Hot")
    warm = sum(1 for l in leads if l["tier"] == "Warm")
    subject = (f"{len(leads)} new off-market lead{'s' if len(leads) != 1 else ''}"
               + (f" ({hot} Hot)" if hot else "")) if leads else "No new off-market leads today"

    lines, rows = [], []
    for l in leads[:MAX_ROWS]:
        os_ = owners.get(l["id"], [])
        owner = "; ".join(o["name"] for o in os_) or "Owner not in deed records"
        mail = next((o["mailing_address"] for o in os_ if o["mailing_address"]), "")
        addr = l["address"] + (f" #{l['unit']}" if l["unit"] else "")
        off = l["off_date"].strftime("%b %d") if l["off_date"] else ""
        link = f"{site}/listing/{l['id']}"
        lines.append(f"[{l['tier']} {l['score']}] {addr}, {l['neighborhood'] or l['borough']} | "
                     f"{_money(l['list_price'])} | off {off} | {owner}{' | ' + mail if mail else ''}\n  {link}")
        e = html.escape
        rows.append(
            f"<tr><td style='padding:8px;border-bottom:1px solid #eee'><b style='color:"
            f"{'#b4410f' if l['tier'] == 'Hot' else '#946200' if l['tier'] == 'Warm' else '#555'}'>"
            f"{e(l['tier'] or '')} {l['score']}</b></td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'><a href='{e(link)}'><b>{e(addr)}</b></a><br>"
            f"<span style='color:#666'>{e(l['neighborhood'] or l['borough'] or '')} · {e(l['property_type'] or '')}</span></td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'>{_money(l['list_price'])}<br>"
            f"<span style='color:#666'>off {e(off)} ({e(l['off_reason'] or '')})</span></td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'>{e(owner)}"
            f"{'<br><span style=color:#666>' + e(mail) + '</span>' if mail else ''}</td></tr>")
    more = len(leads) - MAX_ROWS
    text = (f"{len(leads)} new leads ({hot} Hot, {warm} Warm).\n\n" + "\n\n".join(lines)
            + (f"\n\n…and {more} more on the site." if more > 0 else "") + f"\n\nAll leads: {site}/\n") \
        if leads else f"Nothing new came off the market overnight.\n\nAll leads: {site}/\n"
    body = (f"<div style='font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;color:#1c1b19'>"
            f"<h2 style='font-family:Georgia,serif;font-weight:600'>{html.escape(subject)}</h2>")
    if leads:
        body += (f"<p>{hot} Hot, {warm} Warm. Best leads first.</p><table style='border-collapse:collapse;width:100%'>"
                 + "".join(rows) + "</table>"
                 + (f"<p>…and {more} more on the site.</p>" if more > 0 else ""))
    else:
        body += "<p>Nothing new came off the market overnight.</p>"
    body += f"<p><a href='{html.escape(site)}/'>Open all leads</a></p></div>"
    return subject, text, body


def send(subject, text, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.DIGEST_FROM or config.SMTP_USER
    msg["To"] = config.DIGEST_TO
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def run():
    if not enabled():
        return {"skipped": "email not set up (SMTP_USER / SMTP_PASSWORD / DIGEST_TO)"}
    with db.engine().connect() as conn:
        leads = new_leads(conn)
        subject, text, body = build(conn, leads)
    send(subject, text, body)
    with db.engine().begin() as conn:
        now = db.now()
        for l in leads:
            conn.execute(db.digest_sent.insert().values(listing_id=l["id"], sent_at=now))
    return {"emailed": len(leads), "to": config.DIGEST_TO}
