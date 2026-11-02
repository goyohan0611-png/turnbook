"""The brain's current understanding: short state lines every attached agent reads the same way.

docs/design/state-layer.md. A state line is a saved memory (memory_type "state") worked out from
turns it cites in attributes.from_turns, so forgetting those turns takes it too (forget.py).
Kinds: value (a current value with what it replaced), event (something that happened, dated),
item (one member of a group that spans sessions). This module turns a line into the short text
a packet carries, and maps it onto the memory model.
"""
from __future__ import annotations

KINDS = {"value", "event", "item"}
MAX_TEXT = 220                       # one line in a packet; the turns hold the rest


def compact(line: dict) -> str:
    """`old sneakers: shoe rack (since 2023-05-29; before: under the bed, 2023-05-25)`."""
    subject = str(line.get("subject") or "").strip()
    attribute = str(line.get("attribute") or "").strip()
    value = " ".join(str(line.get("value") or "").split())
    kind = line.get("kind")
    head = subject if not attribute or subject.lower() in {"user", "me", "i"} else \
        f"{subject} {attribute}"
    if kind == "value":
        head = attribute or subject
    text = f"{head}: {value}" if head else value
    if kind == "value":
        notes = [f"since {line['as_of']}"] if line.get("as_of") else []
        earlier = [f"{' '.join(str(v).split())}, {when}"
                   for v, when in (line.get("replaced") or [])[-2:]]
        if earlier:
            notes.append("before: " + "; ".join(earlier))
        if notes:
            text += f" ({'; '.join(notes)})"
    elif kind == "event" and line.get("date"):
        text += f" ({line['date']})"
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT - 1].rstrip() + "…"
    return text


if __name__ == "__main__":           # the check this module leaves behind
    assert compact({"subject": "User", "kind": "value", "attribute": "old sneaker storage",
                    "value": "shoe rack in the closet", "as_of": "2023-05-29",
                    "replaced": [["under the bed", "2023-05-25"]]}) == \
        "old sneaker storage: shoe rack in the closet (since 2023-05-29; before: under the " \
        "bed, 2023-05-25)"
    assert compact({"subject": "trips", "kind": "event", "value": "Jeju, 3 nights",
                    "date": "2023-04-11"}) == "trips: Jeju, 3 nights (2023-04-11)"
    assert compact({"subject": "bikes owned", "kind": "item", "value": "road bike"}) == \
        "bikes owned: road bike"
    assert len(compact({"kind": "item", "subject": "x", "value": "y" * 500})) == MAX_TEXT
    print("ok")
