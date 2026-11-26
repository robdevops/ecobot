"""An alert log as a feed for signalbot (its docs/FEEDS.md): what ecobot://feed and irrigation://feed read, so signalbot posts the
alerts to the chats that subscribe, with no client of its own (docs/ecobot-mcp.md 3.6).

{identity, kinds: [{kind, label, words}], items: [{id, kind, time, expires, text, [link], [chart]}]}: the log's identity; the kinds the
server can send now (by its sources), each with its button's label and the words people use for it; the alerts not yet expired, oldest
first, the newest MAX_ITEMS. An item's `link` is its link's URL. Its `chart` is the call that draws it, its drawn_by's: {tool, arguments},
with `arg` "period" and `values` its periods when the call names a period that is one of them, so a client can offer them as buttons."""

from .alerts.kinds import LABELS, WORDS

MAX_ITEMS = 1000   # signalbot reads the newest 1,000 listed


def feed_read(alert_log, kinds) -> dict:
    """The feed of `alert_log`, offering `kinds` (in order)."""
    events = alert_log.since(0)[-MAX_ITEMS:]
    return {"identity": alert_log.identity,
            "kinds": [{"kind": k, "label": _label(k), "words": WORDS.get(k, [])} for k in kinds],
            "items": [_item(e) for e in events]}


def _label(kind: str) -> str:
    label = LABELS.get(kind, kind)
    return label[:1].upper() + label[1:]


def _item(event: dict) -> dict:
    item = {"id": str(event["id"]), "kind": event["kind"], "time": event["time"], "text": event.get("text") or event.get("title") or ""}
    if event.get("expires"):
        item["expires"] = event["expires"]
    link = event.get("link")
    if isinstance(link, dict) and str(link.get("url", "")).startswith(("https://", "http://")):
        item["link"] = link["url"]
    if chart := _chart(event.get("chart")):
        item["chart"] = chart
    return item


def _chart(chart) -> dict | None:
    """The call that draws an alert's chart, from its drawn_by; None without one."""
    drawn_by = chart.get("drawn_by") if isinstance(chart, dict) else None
    if not isinstance(drawn_by, dict) or not drawn_by.get("tool") or not isinstance(drawn_by.get("arguments"), dict):
        return None
    out = {"tool": drawn_by["tool"], "arguments": drawn_by["arguments"]}
    values = [p["period"] for p in drawn_by.get("periods") or [] if isinstance(p, dict) and isinstance(p.get("period"), str)]
    if values and drawn_by["arguments"].get("period") in values:
        out.update(arg="period", values=values)
    return out
