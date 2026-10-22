"""Tiny client for the Socrata (SODA) APIs behind NYC Open Data and data.ny.gov."""
import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)
PAGE = 5000


def query(domain, dataset, where=None, order=None, select=None, limit=50000, extra=None):
    """Yield rows from a Socrata dataset, paging until done or `limit` rows."""
    url = f"https://{domain}/resource/{dataset}.json"
    headers = {"Accept": "application/json"}
    if config.SOCRATA_APP_TOKEN:
        headers["X-App-Token"] = config.SOCRATA_APP_TOKEN
    offset = 0
    while offset < limit:
        params = {"$limit": min(PAGE, limit - offset), "$offset": offset}
        if where:
            params["$where"] = where
        if order:
            params["$order"] = order
        if select:
            params["$select"] = select
        if extra:
            params.update(extra)
        rows = _get(url, params, headers)
        if not rows:
            return
        yield from rows
        if len(rows) < params["$limit"]:
            return
        offset += len(rows)


def _get(url, params, headers, attempts=4):
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if i == attempts - 1:
                raise
            wait = 2 ** (i + 1)
            log.warning("retrying %s in %ss: %s", url, wait, e)
            time.sleep(wait)


def soql_date(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")
