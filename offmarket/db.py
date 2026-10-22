"""Database tables. SQLite locally, Postgres on a host (set DATABASE_URL)."""
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine,
)

from . import config

metadata = MetaData()

# One row per listing (per feed listing id). `state` is what the site is about:
#   active          on the market, being watched
#   pulled          went off the market and no sale has been found  -> a lead
#   contract_off    was in contract, then left the market; waiting to see if a sale records
#   sold            closed in the feed, or a sale is recorded in ACRIS / DOF
#   relisted        came back on the market (same or another broker)
listings = Table(
    "listings", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(40), nullable=False),
    Column("listing_key", String(120), nullable=False),
    Column("listing_url", String(500)),
    Column("address", String(300)),           # street address without unit
    Column("unit", String(40)),
    Column("norm_address", String(300), index=True),
    Column("norm_unit", String(40)),
    Column("borough", String(20)),
    Column("neighborhood", String(80)),
    Column("zip", String(10)),
    Column("property_type", String(30)),      # Condo | Co-op | Townhouse | Single family | Multi-family | Other
    Column("beds", Float),
    Column("baths", Float),
    Column("sqft", Integer),
    Column("list_price", Integer),            # last asking price
    Column("original_price", Integer),        # first asking price we know of
    Column("high_price", Integer),            # highest asking price seen
    Column("price_cuts", Integer, default=0),
    Column("list_date", DateTime),
    Column("feed_status", String(40)),        # status as the feed last reported it
    Column("state", String(20), index=True, default="active"),
    Column("off_reason", String(40)),         # Expired | Withdrawn | Canceled | Off market | Disappeared
    Column("off_date", DateTime),
    Column("days_on_market", Integer),
    Column("was_in_contract", Boolean, default=False),
    Column("prior_pulls", Integer, default=0),  # earlier listings of the same unit that also came off unsold
    Column("list_agent", String(200)),
    Column("list_office", String(200)),
    Column("list_agent_phone", String(40)),
    Column("list_agent_email", String(200)),
    Column("remarks", Text),
    Column("photo_url", String(500)),
    Column("bbl", String(12), index=True),
    Column("lat", Float),
    Column("lon", Float),
    Column("sale_checked_at", DateTime),
    Column("sale_check_note", String(300)),
    Column("sold_price", Integer),
    Column("sold_date", DateTime),
    Column("sold_source", String(60)),
    Column("owner_checked_at", DateTime),
    Column("skiptraced_at", DateTime),
    Column("score", Integer, default=0),
    Column("tier", String(10)),
    Column("score_reasons", JSON),
    Column("lead_status", String(20), default="New"),  # New | Contacted | Meeting | Signed | Dismissed
    Column("notes", Text, default=""),
    Column("first_seen", DateTime),
    Column("last_seen_active", DateTime),
    Column("updated_at", DateTime),
    Column("raw", JSON),
    UniqueConstraint("source", "listing_key", name="uq_listing"),
)
Index("ix_listings_state_score", listings.c.state, listings.c.score)

# Price and status history, one row per change we observe.
events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True),
    Column("listing_id", Integer, ForeignKey("listings.id"), index=True),
    Column("at", DateTime),
    Column("kind", String(20)),       # listed | price | status | off | sold | relisted | note
    Column("detail", String(300)),
    Column("price", Integer),
)

# Owners of record from ACRIS (and LLC details from NY DOS).
owners = Table(
    "owners", metadata,
    Column("id", Integer, primary_key=True),
    Column("listing_id", Integer, ForeignKey("listings.id"), index=True),
    Column("name", String(300)),
    Column("is_entity", Boolean, default=False),   # LLC / trust / corp
    Column("mailing_address", String(400)),
    Column("acquired_date", DateTime),
    Column("acquired_price", Integer),
    Column("doc_id", String(40)),
    Column("doc_type", String(20)),
    Column("entity_people", JSON),   # [{name, role, address}] from NY DOS for LLCs
    Column("source", String(60)),
)

# Phones / emails from skip tracing, with Do Not Call flags.
contacts = Table(
    "contacts", metadata,
    Column("id", Integer, primary_key=True),
    Column("listing_id", Integer, ForeignKey("listings.id"), index=True),
    Column("person", String(200)),
    Column("kind", String(10)),      # phone | email
    Column("value", String(200)),
    Column("phone_type", String(20)),
    Column("dnc", Boolean, default=False),       # on the National Do Not Call registry
    Column("tcpa", Boolean, default=False),      # known litigator / TCPA blacklist
    Column("suppressed", Boolean, default=False),  # on our own suppression list
    Column("source", String(40)),
    Column("created_at", DateTime),
)

# Recorded documents checked for a sale (kept so the lead page can show the proof).
sale_docs = Table(
    "sale_docs", metadata,
    Column("id", Integer, primary_key=True),
    Column("listing_id", Integer, ForeignKey("listings.id"), index=True),
    Column("doc_id", String(40)),
    Column("doc_type", String(20)),
    Column("doc_date", DateTime),
    Column("recorded", DateTime),
    Column("amount", Integer),
    Column("unit", String(40)),
    Column("parties_from", String(400)),
    Column("parties_to", String(400)),
    Column("counts_as_sale", Boolean),
    Column("source", String(40)),
    UniqueConstraint("listing_id", "doc_id", name="uq_sale_doc"),
)

# Do-not-contact list: NY DOS cease-and-desist owners, people who asked not to be contacted, etc.
suppression = Table(
    "suppression", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String(10)),      # phone | email | name | address
    Column("value", String(300)),
    Column("norm", String(300), index=True),
    Column("reason", String(200)),
    Column("created_at", DateTime),
)

settings = Table(
    "settings", metadata,
    Column("key", String(60), primary_key=True),
    Column("value", JSON),
)

runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("step", String(40), index=True),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("status", String(20)),    # ok | error | skipped
    Column("message", Text),
    Column("stats", JSON),
)

_engine = None


def engine():
    global _engine
    if _engine is None:
        if config.DATABASE_URL.startswith("sqlite:///"):
            Path(config.DATABASE_URL.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(config.DATABASE_URL, future=True, pool_pre_ping=True)
        metadata.create_all(_engine)
    return _engine


def reset_engine():
    global _engine
    _engine = None


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)
