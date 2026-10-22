"""Listing feed over the RESO Web API (OData), the industry-standard MLS data API.

REBNY's RLS distributes its data through Trestle (Cotality, formerly CoreLogic); OneKey MLS, Bridge and
MLS Grid speak the same API. A brokerage gets credentials through RLS ("Back Office" or "Product" feed);
a Back Office feed includes off-market statuses (Expired, Withdrawn...), an IDX feed only has active
listings, in which case the pipeline spots listings that disappear.
"""
import logging
import time
from datetime import datetime

import requests

from .. import config
from . import base

log = logging.getLogger(__name__)
name = "reso"
label = f"{config.RESO_FEED_NAME} (RESO Web API)"


def enabled():
    return bool(config.RESO_BASE_URL and (config.RESO_ACCESS_TOKEN or (config.RESO_CLIENT_ID and
                                                                       config.RESO_CLIENT_SECRET)))


_token = {"value": None, "exp": 0}


def _auth_header():
    if config.RESO_ACCESS_TOKEN:
        return {"Authorization": f"Bearer {config.RESO_ACCESS_TOKEN}"}
    if not _token["value"] or time.time() > _token["exp"] - 60:
        r = requests.post(config.RESO_TOKEN_URL, data={
            "grant_type": "client_credentials", "client_id": config.RESO_CLIENT_ID,
            "client_secret": config.RESO_CLIENT_SECRET, "scope": config.RESO_SCOPE}, timeout=60)
        r.raise_for_status()
        j = r.json()
        _token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)))
    return {"Authorization": f"Bearer {_token['value']}"}


def _get(url, params=None, attempts=4):
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers={**_auth_header(), "Accept": "application/json"},
                             timeout=120)
            if r.status_code == 404:
                return None
            if r.status_code == 401 and not config.RESO_ACCESS_TOKEN:
                _token["value"] = None
                raise requests.HTTPError("401, refreshing token")
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} from feed")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if i == attempts - 1:
                raise
            time.sleep(2 ** (i + 1))
            log.warning("feed retry: %s", e)


def _query(filt, page=200, limit=20000):
    url = config.RESO_BASE_URL.rstrip("/") + "/Property"
    if config.RESO_EXTRA_FILTER:
        filt = f"({filt}) and ({config.RESO_EXTRA_FILTER})"
    params = {"$filter": filt, "$top": page, "$orderby": "ListingKey"}
    seen = 0
    skip = 0
    while url and seen < limit:
        j = _get(url, params)
        if not j:
            return
        rows = j.get("value", [])
        for r in rows:
            yield r
        seen += len(rows)
        nxt = j.get("@odata.nextLink")
        if nxt:
            url, params = nxt, None
        elif len(rows) == page and params is not None:
            skip += page
            params = {**params, "$skip": skip}
        else:
            return


def _base_filter(min_price):
    return (f"(ListPrice ge {min_price} or OriginalListPrice ge {min_price}) and "
            f"(PropertyType eq 'Residential' or PropertyType eq 'ResidentialIncome')")


def fetch_active(min_price):
    """Everything on the market now (Active / Coming soon / In contract) over the price floor."""
    status_f = "(StandardStatus eq 'Active' or StandardStatus eq 'ActiveUnderContract' or " \
               "StandardStatus eq 'Pending' or StandardStatus eq 'ComingSoon')"
    for r in _query(f"{_base_filter(min_price)} and {status_f}"):
        yield to_listing(r)


def fetch_changed(min_price, since: datetime):
    """Every listing over the floor changed since `since`, any status (Expired, Withdrawn, Closed...)."""
    ts = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in _query(f"{_base_filter(min_price)} and ModificationTimestamp gt {ts}"):
        yield to_listing(r)


def fetch_one(listing_key):
    j = _get(config.RESO_BASE_URL.rstrip("/") + f"/Property('{listing_key}')")
    if not j:
        return None
    if "value" in j:
        j = j["value"][0] if j["value"] else None
    return to_listing(j) if j else None


def to_listing(r):
    g = r.get
    street = g("UnparsedAddress") or " ".join(str(x) for x in [g("StreetNumber"), g("StreetDirPrefix"),
                                                                 g("StreetName"), g("StreetSuffix"),
                                                                 g("StreetDirSuffix")] if x)
    std = base.status(g("StandardStatus"))
    mls = base.status(g("MlsStatus"))
    # MlsStatus is more specific than StandardStatus for off-market flavours (e.g. Withdrawn vs Canceled).
    st = mls if (std in base.PULLED | {base.UNKNOWN} and mls != base.UNKNOWN) else std
    off_date = (g("OffMarketDate") or g("WithdrawnDate") or g("CancellationDate") or g("ExpirationDate")) \
        if st in base.PULLED else None
    key = str(g("ListingKey") or g("ListingId") or "")
    return base.listing(
        source=name, listing_key=key, url=g("ListingURL") or g("VirtualTourURLUnbranded"),
        address=street, unit=g("UnitNumber") or "",
        borough=base.borough_from(g("City"), g("CountyOrParish"), g("PostalCode")),
        neighborhood=g("SubdivisionName") or g("MLSAreaMinor") or g("MLSAreaMajor") or "",
        zip=g("PostalCode") or "",
        property_type=base.property_type(g("PropertySubType"), g("StructureType") and str(g("StructureType")),
                                         g("OwnershipType"), g("PropertyType")),
        beds=g("BedroomsTotal"), baths=g("BathroomsTotalInteger") or g("BathroomsFull"),
        sqft=int(g("LivingArea")) if g("LivingArea") else None,
        list_price=_int(g("ListPrice")), original_price=_int(g("OriginalListPrice")),
        close_price=_int(g("ClosePrice")), close_date=base.parse_date(g("CloseDate")),
        list_date=base.parse_date(g("ListingContractDate") or g("OnMarketDate")),
        status=st, status_date=base.parse_date(off_date or g("StatusChangeTimestamp") or g("ModificationTimestamp")),
        agent=g("ListAgentFullName"), office=g("ListOfficeName"),
        agent_phone=g("ListAgentDirectPhone") or g("ListAgentOfficePhone"), agent_email=g("ListAgentEmail"),
        remarks=g("PublicRemarks"), bbl=base.bbl_from(g("ParcelNumber")),
        lat=g("Latitude"), lon=g("Longitude"),
        days_on_market=g("CumulativeDaysOnMarket") or g("DaysOnMarket"),
        raw={k: v for k, v in r.items() if v not in (None, "", [])},
    )


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
