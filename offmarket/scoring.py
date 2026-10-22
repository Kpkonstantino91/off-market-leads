"""How promising a pulled listing is as a listing pitch. Hot >= 60, Warm >= 40, else Watch.
Every point is listed on the lead page so the ranking is never a mystery."""
from datetime import datetime


def score(l, owners=(), contacts=(), today=None):
    today = today or datetime.utcnow()
    pts = []

    def add(n, why):
        if n:
            pts.append([n, why])

    price = l.get("high_price") or l.get("list_price") or 0
    add(10 if price < 8e6 else 15 if price < 12e6 else 20 if price < 20e6 else 25, f"Asking ${price / 1e6:.1f}M")

    if l.get("off_date"):
        days = (today - l["off_date"]).days
        add(25 if days <= 7 else 18 if days <= 30 else 10 if days <= 90 else 4, f"Off the market {days} days ago")

    reason = l.get("off_reason")
    if reason == "Expired":
        add(8, "Listing expired: the exclusive has ended")
    elif reason in ("Withdrawn", "Canceled"):
        add(5, f"Listing {reason.lower()}")
    elif reason == "Off market":
        add(-5, "Marked temporarily off market: may still be under an exclusive")

    dom = l.get("days_on_market") or 0
    if dom >= 365:
        add(12, f"{dom} days on market")
    elif dom >= 180:
        add(8, f"{dom} days on market")
    elif dom >= 90:
        add(4, f"{dom} days on market")

    cuts = l.get("price_cuts") or 0
    if cuts:
        add(min(12, cuts * 4), f"{cuts} price cut{'s' if cuts > 1 else ''}")
    hi, last = l.get("high_price") or 0, l.get("list_price") or 0
    if hi and last and last < hi and (hi - last) / hi >= 0.1:
        add(5, f"Price cut {round(100 * (hi - last) / hi)}% overall")

    if l.get("prior_pulls"):
        add(min(15, 10 * l["prior_pulls"]), f"Failed to sell {l['prior_pulls'] + 1} times")
    if l.get("was_in_contract"):
        add(4, "Went into contract and fell through")

    if owners:
        if any(not o.get("is_entity") for o in owners):
            add(5, "Owner is a named individual")
        elif any(o.get("entity_people") for o in owners):
            add(2, "LLC owner with people on file at NY DOS")
    callable_ = [c for c in contacts if c.get("kind") == "phone" and not (c.get("dnc") or c.get("tcpa")
                                                                          or c.get("suppressed"))]
    if callable_:
        add(10, "Callable phone (not on Do Not Call)")
    elif any(c.get("kind") == "email" and not c.get("suppressed") for c in contacts):
        add(6, "Owner email found")
    elif any(o.get("mailing_address") for o in owners):
        add(3, "Owner mailing address on record")

    total = max(0, min(100, sum(p[0] for p in pts)))
    tier = "Hot" if total >= 60 else "Warm" if total >= 40 else "Watch"
    return total, tier, pts
