"""Address, unit, name and phone clean-up so feeds, ACRIS and DOF records line up."""
import re

_SUFFIX = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AVE", "AV": "AVE", "PLACE": "PL", "ROAD": "RD", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "LANE": "LN", "TERRACE": "TER", "COURT": "CT", "SQUARE": "SQ", "PARKWAY": "PKWY",
    "HIGHWAY": "HWY", "PLAZA": "PLZ", "SLIP": "SLIP", "WALK": "WALK", "ALLEY": "ALY", "CIRCLE": "CIR",
}
_DIR = {"EAST": "E", "WEST": "W", "NORTH": "N", "SOUTH": "S"}
_WORDS = {"FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5", "SIXTH": "6",
          "SEVENTH": "7", "EIGHTH": "8", "NINTH": "9", "TENTH": "10", "ELEVENTH": "11", "TWELFTH": "12"}
_SAINT = {"ST.": "SAINT"}
BOROUGH_CODES = {"manhattan": "1", "bronx": "2", "brooklyn": "3", "queens": "4", "staten island": "5"}
CODE_BOROUGH = {v: k.title() for k, v in BOROUGH_CODES.items()}


def street(s):
    """'15 Central Park West' -> '15 CENTRAL PARK W'; 'East 72nd Street' -> 'E 72 ST'."""
    if not s:
        return ""
    s = s.upper().replace(".", " ").replace(",", " ")
    s = re.sub(r"(\d+)(ST|ND|RD|TH)\b", r"\1", s)
    out = []
    toks = s.split()
    for i, t in enumerate(toks):
        t = _WORDS.get(t, t)
        last = i == len(toks) - 1
        if t in _DIR and (i <= 1 or last):
            t = _DIR[t]
        elif t in _SUFFIX:
            t = _SUFFIX[t]
        out.append(t)
    return " ".join(out)


def split_number(addr):
    """'15 Central Park West' -> ('15', 'CENTRAL PARK W'). Handles '1-3 Main St' and '35-37'."""
    n = street(addr)
    m = re.match(r"^(\d+[A-Z]?(?:-\d+[A-Z]?)?)\s+(.*)$", n)
    if not m:
        return "", n
    return m.group(1), m.group(2)


def unit(u):
    """'Apt 12A', '#12-A', 'Unit PH 3' -> '12A', '12A', 'PH3'."""
    if not u:
        return ""
    u = u.upper()
    u = re.sub(r"^(APT|APARTMENT|UNIT|STE|SUITE|NO|#)\.?\s*", "", u.strip())
    u = re.sub(r"[^A-Z0-9]", "", u)
    return u.lstrip("0") or u


def split_unit(addr):
    """Pull a unit off the end of a one-line address: '200 E 66th St #PH-B' -> ('200 E 66th St', 'PH-B')."""
    if not addr:
        return "", ""
    m = re.search(r"\s*(?:,\s*)?(?:#|\bAPT\.?|\bUNIT|\bSUITE|\bPH\b)\s*([\w-]*)\s*$", addr, re.I)
    if m:
        u = m.group(1)
        if re.search(r"\bPH\b", m.group(0), re.I) and not re.search(r"#|APT|UNIT", m.group(0), re.I):
            u = "PH" + u
        return addr[:m.start()].strip().rstrip(","), u
    return addr.strip(), ""


def norm_address(addr):
    return street(addr)


_ENTITY = re.compile(r"\b(LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO|LP|L\.P|LLP|TRUST|TRUSTEE|TRUSTEES|"
                     r"FOUNDATION|PARTNERS|HOLDINGS|ASSOCIATES|REALTY|PROPERTIES|ESTATE|BANK|FUND)\b\.?", re.I)


def is_entity(name):
    return bool(_ENTITY.search(name or ""))


def person_name(acris_name):
    """ACRIS stores 'SMITH, JOHN A' -> 'John A Smith'. Entities are returned title-cased as-is."""
    n = (acris_name or "").strip()
    if not n:
        return ""
    if is_entity(n) or "," not in n:
        return _title(n)
    last, first = n.split(",", 1)
    return _title(f"{first.strip()} {last.strip()}")


def _title(s):
    keep = {"LLC", "LP", "LLP", "INC", "II", "III", "IV", "NY", "USA"}
    return " ".join(w if w.upper() in keep else w.capitalize() for w in s.split())


def phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def fmt_phone(p):
    d = phone(p)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else (p or "")


def price(v):
    if v in (None, ""):
        return None
    try:
        return int(round(float(str(v).replace("$", "").replace(",", "").strip())))
    except ValueError:
        return None


def suppression_norm(kind, value):
    if kind == "phone":
        return phone(value)
    if kind == "email":
        return (value or "").strip().lower()
    if kind == "address":
        a, u = split_unit(value)
        return (street(a) + (" #" + unit(u) if u else "")).strip()
    return re.sub(r"[^A-Z ]", "", (value or "").upper()).strip()
