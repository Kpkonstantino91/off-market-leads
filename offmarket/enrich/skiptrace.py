"""Owner phones and emails by property address, via BatchData's skip-trace API (pay per match).

Off until BATCHDATA_API_KEY is set. Every phone comes back with Do Not Call (dnc) and TCPA-litigator
(tcpa) flags, which the site shows and honours.
"""
import logging

import requests

from .. import config, normalize

log = logging.getLogger(__name__)
name = "batchdata"
label = "BatchData skip trace"


def enabled():
    return bool(config.BATCHDATA_API_KEY)


def lookup(address, unit, borough, zip_code):
    street = f"{address} {('#' + unit) if unit else ''}".strip()
    city = "New York" if (borough or "").lower() == "manhattan" else (borough or "New York")
    body = {"requests": [{"propertyAddress": {"street": street, "city": city, "state": "NY", "zip": zip_code or ""}}]}
    r = requests.post(config.BATCHDATA_URL, json=body, timeout=60,
                      headers={"Authorization": f"Bearer {config.BATCHDATA_API_KEY}",
                               "Content-Type": "application/json", "Accept": "application/json"})
    r.raise_for_status()
    return parse(r.json())


def parse(j):
    """-> [{person, kind, value, phone_type, dnc, tcpa}]"""
    out = []
    results = (j.get("results") or {})
    persons = results.get("persons")
    if persons is None:  # some versions nest per request
        persons = [p for res in (results.get("results") or results.get("data") or []) for p in res.get("persons", [])]
    for p in persons or []:
        nm = p.get("name") or {}
        person = " ".join(x for x in [nm.get("first"), nm.get("middle"), nm.get("last")] if x) or nm.get("full", "")
        for ph in p.get("phoneNumbers") or []:
            num = normalize.phone(ph.get("number"))
            if num:
                out.append({"person": person, "kind": "phone", "value": num, "phone_type": ph.get("type"),
                            "dnc": bool(ph.get("dnc")), "tcpa": bool(ph.get("tcpa"))})
        for em in p.get("emails") or []:
            e = em.get("email") if isinstance(em, dict) else em
            if e:
                out.append({"person": person, "kind": "email", "value": e.lower(), "phone_type": None,
                            "dnc": False, "tcpa": False})
    return out
