from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class Appointment:
    title: str
    label: str
    state: str
    start: float
    identity: tuple | None


def snapshot_items(data, now):
    """Mark out-of-day occurrences so a newer reschedule can supersede a stale invite."""
    today = now.date()
    if not data or not data.get("first", "") <= today.isoformat() < data.get("until", ""):
        return None
    events = data.get("events")
    if not isinstance(events, list):
        return None
    midnight = datetime.combine(today, time.min, now.tzinfo).timestamp()
    instant = now.timestamp()
    items = []
    for event in events:
        if event["allDay"]:
            start = date.fromisoformat(event["start"])
            end = date.fromisoformat(event["end"]) if event.get("end") else None
            state = "all_day" if start == today or (end and start < today < end) else "outside"
            label, timestamp, occurrence = "Tutto il giorno", 0, start.isoformat()
        else:
            start = datetime.fromisoformat(event["start"]).astimezone(now.tzinfo)
            end = datetime.fromisoformat(event.get("end", event["start"])).timestamp()
            timestamp = start.timestamp()
            label = start.strftime("%H:%M") if start.date() == today else "Da ieri"
            if start.date() != today and not (start.date() < today and end > midnight):
                state = "outside"
            elif timestamp <= instant < end or timestamp == instant == end:
                state = "ongoing"
            else:
                state = "upcoming" if timestamp > instant else "past"
            occurrence = timestamp
        uid = event.get("uid")
        original = event.get("occurrence")
        if original and "T" in original:
            original_dt = datetime.fromisoformat(original)
            if original_dt.tzinfo is None:
                original_dt = original_dt.replace(tzinfo=now.tzinfo)
            occurrence = original_dt.timestamp()
        elif original:
            occurrence = original
        identity = (uid, occurrence) if uid else None
        title = " ".join(event["title"].split())[:512] or "Senza titolo"
        items.append(Appointment(title, label, state, timestamp, identity))
    return items


@dataclass
class DailyAgenda:
    items: list
    total: int
    covered: int
    stale: int
    oldest: datetime | None

    def status(self):
        if not self.covered:
            return "Calendari non disponibili"
        stamp = self.oldest.strftime("%d/%m %H:%M")
        if self.total == 1:
            return ("Dati salvati " if self.stale else "Aggiornato ") + stamp
        status = f"Calendari disponibili {self.covered}/{self.total}"
        if self.stale:
            status += f" · {self.stale} " + ("copia offline" if self.stale == 1 else "copie offline")
        return status + f" · Agg. meno recente {stamp}"


def combine_agendas(snapshots, now):
    named = {}
    anonymous = []
    stamps = []
    stale_count = 0
    for data, stamp, stale in snapshots:
        try:
            items = snapshot_items(data, now)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            # A damaged saved calendar must not describe today as empty or
            # suppress events supplied by the other configured calendars.
            continue
        if items is None or stamp is None:
            continue
        stamps.append(stamp)
        stale_count += int(stale)
        for item in items:
            if item.identity is None:
                anonymous.append(item)
                continue
            rank = (stamp.timestamp(), not stale)
            prior = named.get(item.identity)
            if prior is None or rank > prior[0]:
                named[item.identity] = rank, item
    anonymous.extend(item for _, item in named.values())
    items = [item for item in anonymous if item.state != "outside"]
    upcoming = min((item.start for item in items if item.state == "upcoming"), default=None)

    def order(item):
        if item.state == "ongoing":
            return 0, item.start, item.title
        if item.state == "upcoming" and item.start == upcoming:
            return 1, item.start, item.title
        if item.state == "all_day":
            return 2, 0, item.title
        if item.state == "upcoming":
            return 3, item.start, item.title
        return 4, -item.start, item.title

    return DailyAgenda(sorted(items, key=order), len(snapshots), len(stamps), stale_count,
                       min(stamps) if stamps else None)
