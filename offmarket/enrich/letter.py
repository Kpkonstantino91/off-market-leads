"""Outreach letter for an owner whose listing came off unsold. A plain template by default;
Claude writes a tailored version when ANTHROPIC_API_KEY is set."""
import logging

from .. import config

log = logging.getLogger(__name__)


def _salutation(owners):
    people = [o for o in owners if not o["is_entity"]]
    if people:
        return "Dear " + " and ".join(o["name"] for o in people[:2])
    return "Dear Owner"


def template(l, owners):
    unit = f", {l['unit']}" if l["unit"] else ""
    sign = "\n".join(x for x in [config.AGENT_NAME, config.BROKERAGE_NAME, config.AGENT_PHONE, config.AGENT_EMAIL] if x)
    return (
        f"{_salutation(owners)},\n\n"
        f"I noticed that {l['address']}{unit} recently came off the market. Homes at this level often take a "
        f"second, more targeted campaign to find the right buyer, and the reasons a first listing didn't sell "
        f"are usually fixable: pricing strategy against current comps, presentation, or reaching the private "
        f"buyers who never browse public listing sites.\n\n"
        f"If you are still open to selling, I would welcome 20 minutes to share what we are seeing from buyers "
        f"in {l['neighborhood'] or l['borough']} right now and how we would approach the property differently. "
        f"There is no obligation, and if you have decided to stay, I am glad to be a resource whenever the "
        f"timing changes.\n\n"
        f"Warm regards,\n{sign}\n"
    )


def draft(l, owners, events):
    if not config.ANTHROPIC_API_KEY:
        return template(l, owners), False
    try:
        import anthropic
        history = "\n".join(f"- {e['at']:%b %d, %Y}: {e['detail']}" for e in events if e["at"])
        prompt = (
            "Write a short, discreet letter (under 220 words) from a NYC luxury residential broker to the owner of "
            "a home whose listing came off the market without selling. Tone: respectful, confident, never "
            "pushy, no hype, no mention of how you obtained their name, do not criticise the previous broker by "
            "name. Offer a short conversation. Plain text only.\n\n"
            f"Property: {l['address']} {l['unit'] or ''}, {l['neighborhood'] or l['borough']}; {l['property_type']}; "
            f"{l['beds'] or '?'} bed / {l['baths'] or '?'} bath; last asking ${l['list_price'] or 0:,}.\n"
            f"Listing history:\n{history}\n"
            f"Salutation: {_salutation(owners)}\n"
            f"Sign as: {config.AGENT_NAME or '[Agent name]'}, {config.BROKERAGE_NAME}, "
            f"{config.AGENT_PHONE or '[phone]'}, {config.AGENT_EMAIL or '[email]'}"
        )
        msg = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY).messages.create(
            model=config.CLAUDE_MODEL, max_tokens=800, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip(), True
    except Exception as e:  # noqa: BLE001 - fall back to the template
        log.warning("Claude letter failed: %s", e)
        return template(l, owners), False
