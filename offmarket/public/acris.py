"""ACRIS (NYC Dept. of Finance recorded documents) on NYC Open Data: who owns a unit, and did it sell.

Three datasets joined on document_id:
  Real Property Master  bnx9-e6tj  (doc type, date, amount)
  Real Property Legals  8h5j-fqxa  (borough/block/lot, street address, unit)
  Real Property Parties 636b-3b5g  (grantor/grantee names and addresses)

Condos and townhouses sell by deed (DEED). Co-op apartments aren't real property, but since 2003 a co-op
share transfer is filed as an RPTT return in ACRIS with the unit number, so co-op sales show up too.
Co-op shareholders also appear as debtors on UCC1 financing statements, a fallback for the owner's name.
"""
import logging
from datetime import datetime

from .. import normalize, socrata
from ..sources.base import parse_date

log = logging.getLogger(__name__)
DOMAIN = "data.cityofnewyork.us"
MASTER, LEGALS, PARTIES = "bnx9-e6tj", "8h5j-fqxa", "636b-3b5g"

TRANSFER_TYPES = {"DEED", "DEEDO", "DEED, LE", "DEED, RC", "CORRD", "RPTT", "RPTT&RET", "CONDEED"}
OWNER_FALLBACK_TYPES = {"UCC1"}
DOC_URL = "https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentImageView?doc_id={}"


def doc_url(doc_id):
    return DOC_URL.format(doc_id)


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def find_legals(borough, address, unit, bbl=None):
    """Legals rows (one per document x lot) for this unit. Matches by condo-unit BBL when we have one,
    otherwise by street address + unit (works for co-ops, which share the building's lot)."""
    rows = []
    if bbl and int(bbl[6:]) >= 1001:      # condo unit lots are numbered 1001+
        b, blk, lot = bbl[0], int(bbl[1:6]), int(bbl[6:])
        rows = list(socrata.query(DOMAIN, LEGALS, where=f"borough={b} AND block={blk} AND lot={lot}", limit=2000))
        if rows:
            return rows
    num, street = normalize.split_number(address)
    bcode = normalize.BOROUGH_CODES.get((borough or "").lower())
    if not num or not street or not bcode:
        return []
    # Search on the most distinctive word of the street name, then compare normalised names here.
    key = max((t for t in street.split() if t not in ("E", "W", "N", "S", "ST", "AVE", "PL", "RD", "BLVD")),
              key=len, default=street.split()[0])
    where = f"borough={bcode} AND street_number={_q(num)} AND upper(street_name) like {_q('%' + key + '%')}"
    cand = socrata.query(DOMAIN, LEGALS, where=where, limit=5000)
    want_unit = normalize.unit(unit)
    for r in cand:
        if normalize.street(r.get("street_name")) != street:
            continue
        if want_unit:
            if normalize.unit(r.get("unit")) != want_unit:
                continue
        elif bbl and r.get("lot") and int(r["lot"]) != int(bbl[6:]):
            continue
        rows.append(r)
    return rows


def documents(legal_rows, since=None):
    """Master + party details for the documents behind these legals rows (transfer and UCC1 types only)."""
    by_id = {}
    for r in legal_rows:
        by_id.setdefault(r["document_id"], r)
    ids = list(by_id)
    docs = {}
    types = TRANSFER_TYPES | OWNER_FALLBACK_TYPES
    for i in range(0, len(ids), 50):
        chunk = ",".join(_q(x) for x in ids[i:i + 50])
        where = f"document_id in ({chunk}) AND doc_type in ({','.join(_q(t) for t in types)})"
        for m in socrata.query(DOMAIN, MASTER, where=where, limit=5000):
            d = parse_date(m.get("document_date")) or parse_date(m.get("recorded_datetime"))
            if since and d and d < since:
                continue
            leg = by_id[m["document_id"]]
            docs[m["document_id"]] = {
                "doc_id": m["document_id"], "doc_type": m.get("doc_type"), "doc_date": d,
                "recorded": parse_date(m.get("recorded_datetime")),
                "amount": normalize.price(m.get("document_amt")) or 0,
                "unit": leg.get("unit") or "", "bbl": _bbl(leg), "from": [], "to": [],
            }
    if docs:
        ids = list(docs)
        for i in range(0, len(ids), 50):
            chunk = ",".join(_q(x) for x in ids[i:i + 50])
            for p in socrata.query(DOMAIN, PARTIES, where=f"document_id in ({chunk})", limit=5000):
                d = docs.get(p["document_id"])
                if not d:
                    continue
                addr = ", ".join(x for x in [p.get("address_1"), p.get("address_2"), p.get("city"),
                                             " ".join(y for y in [p.get("state"), p.get("zip")] if y)] if x)
                side = "to" if str(p.get("party_type")) == "2" else "from"
                d[side].append({"name": p.get("name", "").strip(), "address": addr})
    return sorted(docs.values(), key=lambda d: d["doc_date"] or datetime.min, reverse=True)


def _bbl(leg):
    try:
        return f"{int(leg['borough'])}{int(leg['block']):05d}{int(leg['lot']):04d}"
    except (KeyError, ValueError, TypeError):
        return None


def lookup(borough, address, unit, bbl=None):
    return documents(find_legals(borough, address, unit, bbl))


def is_sale(doc, after, min_amount):
    return (doc["doc_type"] in TRANSFER_TYPES and doc["doc_date"] is not None and after is not None
            and doc["doc_date"] >= after and doc["amount"] >= min_amount)


def current_owner(docs):
    """Grantees on the latest transfer (any price, so trust/LLC transfers count); else latest UCC1 debtors."""
    for d in docs:
        if d["doc_type"] in TRANSFER_TYPES and d["to"]:
            return d, d["to"]
    for d in docs:
        if d["doc_type"] in OWNER_FALLBACK_TYPES and d["from"]:
            return d, d["from"]
    return None, []
