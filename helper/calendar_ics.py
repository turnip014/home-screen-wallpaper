"""Minimal iCalendar (.ics) reader for the calendar widget.

Handles what Google, Outlook and iCloud calendar feeds actually use:
all-day and timed events, UTC / TZID / floating times, cancelled events,
EXDATE, moved occurrences (RECURRENCE-ID) and common repeat rules:
DAILY / WEEKLY (BYDAY) / MONTHLY (BYMONTHDAY or nth weekday like 2TU, -1FR) /
YEARLY, with INTERVAL, COUNT and UNTIL. Rules using anything else (BYSETPOS,
BYWEEKNO, ...) only show their first occurrence rather than guessing.
"""

import datetime as dt
import re
from zoneinfo import ZoneInfo

DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MAX_STEPS = 5000


def local_tz():
    return dt.datetime.now().astimezone().tzinfo


def unfold(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def parse_line(line):
    """'DTSTART;TZID=Europe/London:20261124T090000' -> ('DTSTART', {'TZID': ...}, '2026...')"""
    m = re.match(r'^([A-Za-z0-9-]+)((?:;[^:;]+=(?:"[^"]*"|[^:;]*))*):(.*)$', line)
    if not m:
        return None, {}, ""
    params = {}
    for p in re.findall(r';([^=;]+)=("[^"]*"|[^:;]*)', m.group(2)):
        params[p[0].upper()] = p[1].strip('"')
    return m.group(1).upper(), params, m.group(3)


def unescape(text):
    return text.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def parse_time(value, params):
    """Returns (datetime in local time, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = dt.datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=local_tz()), True
    naive = dt.datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        aware = naive.replace(tzinfo=dt.timezone.utc)
    else:
        tz = None
        if "TZID" in params:
            try:
                tz = ZoneInfo(params["TZID"])
            except Exception:  # e.g. Windows zone names in Outlook feeds: treat as local
                tz = None
        aware = naive.replace(tzinfo=tz or local_tz())
    return aware.astimezone(local_tz()), False


def parse_rrule(value):
    return {k.upper(): v for k, v in (part.split("=", 1) for part in value.split(";") if "=" in part)}


def nth_weekday(year, month, weekday, n):
    """n-th (1-based, negative from the end) given weekday of a month, or None."""
    first = dt.date(year, month, 1)
    if n > 0:
        offset = (weekday - first.weekday()) % 7
        day = 1 + offset + (n - 1) * 7
    else:
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        last = nxt - dt.timedelta(days=1)
        day = last.day - ((last.weekday() - weekday) % 7) + (n + 1) * 7
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def add_months(d, n):
    m = d.month - 1 + n
    return d.year + m // 12, m % 12 + 1


def occurrences(start, rule, window_end):
    """Yield start datetimes of a recurring event, in order, up to window_end."""
    freq = rule.get("FREQ")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        until, _ = parse_time(rule["UNTIL"], {})
        if len(rule["UNTIL"].strip()) == 8:  # date-only UNTIL includes that whole day
            until += dt.timedelta(days=1) - dt.timedelta(seconds=1)
    unsupported = set(rule) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "WKST"}
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or unsupported:
        yield start
        return

    emitted = 0

    def ok(t):
        return (until is None or t <= until) and t <= window_end

    if freq == "WEEKLY":
        days = [DAY_CODES.index(d[-2:]) for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAY_CODES]
        days = sorted(days) or [start.weekday()]
        week0 = start - dt.timedelta(days=start.weekday())
        for step in range(MAX_STEPS):
            week = week0 + dt.timedelta(weeks=step * interval)
            for wd in days:
                t = week + dt.timedelta(days=wd)
                if t < start:
                    continue
                if not ok(t) or (count is not None and emitted >= count):
                    return
                emitted += 1
                yield t
        return

    for step in range(MAX_STEPS):
        candidates = []
        if freq == "DAILY":
            candidates = [start + dt.timedelta(days=step * interval)]
        elif freq == "YEARLY":
            try:
                candidates = [start.replace(year=start.year + step * interval)]
            except ValueError:  # Feb 29
                candidates = []
        else:  # MONTHLY
            year, month = add_months(start, step * interval)
            byday = rule.get("BYDAY")
            if byday:
                for token in byday.split(","):
                    m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", token.strip())
                    if m:
                        d = nth_weekday(year, month, DAY_CODES.index(m.group(2)), int(m.group(1) or 1))
                        if d:
                            candidates.append(start.replace(year=d.year, month=d.month, day=d.day))
            else:
                for md in (rule.get("BYMONTHDAY") or str(start.day)).split(","):
                    try:
                        candidates.append(start.replace(year=year, month=month, day=int(md)))
                    except ValueError:
                        pass  # e.g. the 31st in a 30-day month: skipped, as the spec says
        for t in sorted(candidates):
            if t < start:
                continue
            if not ok(t) or (count is not None and emitted >= count):
                return
            emitted += 1
            yield t
        if freq == "DAILY" and candidates and candidates[0] > window_end:
            return


def events_between(ics_text, window_start, window_end):
    """Parse a feed and return event dicts overlapping [window_start, window_end]."""
    lines = unfold(ics_text)
    raw_events, current = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {"exdates": []}
        elif line == "END:VEVENT":
            if current is not None:
                raw_events.append(current)
            current = None
        elif current is not None:
            name, params, value = parse_line(line)
            if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                try:
                    current[name] = parse_time(value, params)
                except ValueError:
                    pass
            elif name == "EXDATE":
                for v in value.split(","):
                    try:
                        current["exdates"].append(parse_time(v, params)[0])
                    except ValueError:
                        pass
            elif name in ("SUMMARY", "LOCATION", "UID", "STATUS", "RRULE", "DURATION"):
                current[name] = value

    # occurrences that were moved/edited individually replace the series' original slot
    overridden = {(e.get("UID"), e["RECURRENCE-ID"][0]) for e in raw_events if "RECURRENCE-ID" in e}

    out = []
    for e in raw_events:
        if e.get("STATUS", "").upper() == "CANCELLED" or "DTSTART" not in e:
            continue
        start, all_day = e["DTSTART"]
        if "DTEND" in e:
            length = e["DTEND"][0] - start
        elif "DURATION" in e:
            length = parse_duration(e["DURATION"])
        else:
            length = dt.timedelta(days=1) if all_day else dt.timedelta(0)
        title = unescape(e.get("SUMMARY", "")) or "(No title)"
        location = unescape(e.get("LOCATION", ""))
        starts = occurrences(start, parse_rrule(e["RRULE"]), window_end) if "RRULE" in e and "RECURRENCE-ID" not in e else [start]
        for s in starts:
            if s in e["exdates"] or (e.get("UID"), s) in overridden:
                continue
            end = s + length
            if end < window_start or s > window_end:
                continue
            out.append({"title": title, "location": location, "allDay": all_day,
                        "start": s.isoformat(), "end": end.isoformat()})
    return out


def parse_duration(value):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return dt.timedelta(0)
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
