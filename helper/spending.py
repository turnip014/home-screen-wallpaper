"""Card spending from your bank's transaction-alert emails (read from Gmail, parsed on this PC).

Most banks don't offer a personal API, but many email an alert for every card
transaction (set the alert threshold to the minimum in your banking app). We read those emails with the read-only
Gmail permission, pick out amount / merchant / card, and keep only the parsed
numbers in spending.json. Nothing is sent anywhere else.

Each purchase is sorted into a category (food delivery, Shopee, subscriptions...), and
a reminder to pay off the card goes on the to-do list at the end of the month.

config.json:
  "spending": {"card_last4": "1234", "budget": 800, "currency": "USD",
               "query": "from:alerts@yourbank.example (transaction OR alert OR card)",  # Gmail search for the alerts
               "pay_for": "previous",            # end of Dec: pay Nov ("current": end of Nov: pay Nov)
               "reminder": true,                 # add the to-do on the due day
               "categories": {"MY GYM": "Fitness"}}   # extra rules: merchant pattern -> category
"""
import datetime as dt
import json
import logging
import os
import re
import threading
import time

import google_sync as gsync

DEFAULT_QUERY = "(\"transaction alert\" OR \"card transaction\") card ending"  # set spending.query to your bank's sender
DAYS = 70
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

AMOUNT = re.compile(r"(?:amount|transaction amount)\s*[:\-]?\s*([A-Z]{3}|[A-Z]{1,2}\$|\$)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
AMOUNT_LOOSE = re.compile(r"\b([A-Z]{3}|[A-Z]{1,2}\$)\s*([\d,]+\.\d{2})\b")
SYMBOLS = {"S$": "SGD", "US$": "USD", "A$": "AUD", "C$": "CAD", "NZ$": "NZD", "HK$": "HKD", "RM": "MYR"}
CARD = re.compile(r"card\s*(?:no\.?|number)?\s*ending(?:\s*(?:with|in))?\s*[:\-]?\s*(\d{4})", re.I)
MERCHANT = re.compile(r"^\s*(?:to|merchant|at)\s*[:\-]\s*(.+?)\s*$", re.I | re.M)
MERCHANT_INLINE = re.compile(r"\bat\s+([A-Z0-9][A-Z0-9 &'.*\-/]{2,40}?)(?:\s+on\s|\.|,|$)")
WHEN = re.compile(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\s*(\d{4})?\s*,?\s*(\d{1,2}:\d{2})", re.I)
NOT_SPEND = re.compile(r"refund|reversal|credited|payment received|otp|one-time password|declined|unsuccessful", re.I)
# declined / blocked attempts (e.g. "Payment Controls Alerts": "Your transaction was unsuccessful ...")
FAILED = re.compile(r"unsuccessful|not successful|declined|rejected|was not (?:completed|approved)", re.I)


def parse(ts, subject, body):
    """One alert email -> {t, amount, currency, merchant, card} or None if it isn't a card spend."""
    text = f"{subject}\n{body}"
    if NOT_SPEND.search(subject) or FAILED.search(body[:1500]) or not re.search(r"card", text, re.I):
        return None
    m = AMOUNT.search(text) or AMOUNT_LOOSE.search(text)
    if not m:
        return None
    cur = m.group(1).upper()
    cur = SYMBOLS.get(cur, cur) if cur != "$" else ""  # "" = the configured currency
    amount = float(m.group(2).replace(",", ""))
    card = CARD.search(text)
    merch = MERCHANT.search(body) or MERCHANT_INLINE.search(body)
    when = ts
    w = WHEN.search(body)
    if w and w.group(2).lower()[:3] in MONTHS:
        sent = dt.datetime.fromtimestamp(ts)
        year = int(w.group(3)) if w.group(3) else sent.year
        h, mi = (int(x) for x in w.group(4).split(":"))
        try:
            d = dt.datetime(year, MONTHS[w.group(2).lower()[:3]], int(w.group(1)), h, mi)
            if d > sent + dt.timedelta(days=2):  # "28 Dec" alert read in January
                d = d.replace(year=year - 1)
            when = d.timestamp()
        except ValueError:
            pass
    return {"t": when, "amount": amount, "currency": cur,
            "merchant": " ".join(merch.group(1).split())[:60] if merch else "Card purchase",
            "card": card.group(1) if card else ""}


# ---- where the money went: first matching rule wins (config "spending.categories" adds rules in front)
CATEGORIES = [
    ("Food delivery", "🛵", r"FOOD ?PANDA|^FP\*|DELIVEROO|GRAB ?FOOD|WHYQ|OBER"),
    ("Google Pay", "📱", r"GOOGLE ?PAY|\bGPAY\b|GOOGLE WALLET"),
    ("Pinduoduo / Temu", "🧧", r"PINDUODUO|\bPDD\b|TEMU"),
    ("Amazon", "📦", r"AMAZON|AMZN"),
    ("Shopee", "🧡", r"SHOPEE"),
    ("Lazada", "💙", r"LAZADA|REDMART"),
    ("Taobao / AliExpress", "🛍️", r"TAOBAO|ALIEXPRESS|ALIBABA|TMALL"),
    ("Subscriptions", "🔁", r"NETFLIX|SPOTIFY|DISNEY|YOUTUBE|APPLE\.COM|ITUNES|ICLOUD|GOOGLE \*|GOOGLE PLAY|GOOGLE ONE|"
                           r"OPENAI|CHATGPT|ANTHROPIC|CLAUDE\.AI|ADOBE|MICROSOFT|MSFT|XBOX|PLAYSTATION|PATREON|"
                           r"PRIME VIDEO|AMAZON PRIME|AUDIBLE|NOTION|CANVA|DROPBOX|GITHUB|NINTENDO|TWITCH|"
                           r"GOMO|SINGTEL|STARHUB|\bM1\b|CIRCLES|SIMBA|GIGA|MYREPUBLIC|VIEWQWEST"),
    ("Transport", "🚕", r"\bGRAB\b|GRAB\*|GRABPAY|GOJEK|\bTADA\b|ZIG|COMFORT|CDG|TRANSIT|SIMPLYGO|\bBUS\b|\bMRT\b|"
                       r"SHELL|ESSO|CALTEX|SPC|SINOPEC|PARKING|CARPARK|EASYPARK"),
    ("Travel", "✈️", r"FLYSCOOT|SCOOT|SINGAPORE AIR|\bSIA\b|AIRASIA|JETSTAR|CATHAY|EMIRATES|AGODA|BOOKING\.COM|"
                    r"EXPEDIA|TRIP\.COM|KLOOK|AIRBNB|HOTEL|RESORT|TRAVELOKA"),
    ("Groceries", "🛒", r"NTUC|FAIRPRICE|COLD STORAGE|\bGIANT\b|SHENG SIONG|DON DON DONKI|DONKI|PRIME SUPERMARKET|"
                       r"MARKET PLACE|HAO MART|SUPERMARKET"),
    ("Dining", "🍜", r"RESTAURANT|CAFE|COFFEE|STARBUCKS|TOAST|KOPI|MCDONALD|\bKFC\b|BURGER|PIZZA|SUSHI|RAMEN|"
                    r"BAKERY|BAKE|DIN TAI FUNG|HAIDILAO|JOLLIBEE|SUBWAY|FOOD|KITCHEN|BISTRO|BAR\b|DESSERT|"
                    r"BUTTER|CREAM|BOBA|BUBBLE TEA|KOI|LIHO|CHICHA|MOTT 32"),
    ("Games & apps", "🎮", r"STEAM|VALVE|EPIC GAMES|RIOT|BLIZZARD|HOYOVERSE|MIHOYO|GOOGLE PLAY APPS|APP STORE"),
    ("Health", "💊", r"GUARDIAN|WATSONS|UNITY PHARM|CLINIC|HOSPITAL|DENTAL|MEDICAL|POLYCLINIC|SNEC"),
]
OTHER = ("Stores & others", "🏬")


def _months_between(a, b):
    return (b.year - a.year) * 12 + b.month - a.month


def _month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def _month_name(key):
    y, m = (int(x) for x in key.split("-"))
    return dt.date(y, m, 1).strftime("%B %Y")


def _last_day(y, m):
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


class Spending:
    def __init__(self, app_dir, get_config, tasks_list=None):
        self.file = os.path.join(app_dir, "spending.json")
        self.get_config = get_config
        self.tasks_list = tasks_list or (lambda: "@default")
        self.lock = threading.Lock()
        try:
            with open(self.file, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}
        if not self.cache.get("since"):  # tracking starts the first time this version runs
            self.cache = {"since": dt.datetime.combine(dt.date.today(), dt.time()).timestamp(), "parsed": {}}
            self._write()
        self.cache.setdefault("parsed", {})
        self.cache.setdefault("paid", {})
        self.cache.setdefault("tasks", {})

    def _write(self):
        with self.lock:
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
            os.replace(tmp, self.file)

    def settings(self):
        c = self.get_config().get("spending") or {}
        return {"card": str(c.get("card_last4") or "").strip(), "budget": float(c.get("budget") or 0),
                "currency": c.get("currency") or "USD", "query": c.get("query") or DEFAULT_QUERY,
                "rules": c.get("categories") or {},
                # 1 = at the end of each month, pay the month before it (Nov spending -> 31 Dec); 0 = pay the month itself
                "lag": 0 if str(c.get("pay_for", "previous")).lower() in ("current", "same", "0") else 1,
                "remind": c.get("reminder", True) is not False}

    # ---- categories
    def _rules(self, s):
        extra = [(str(name), "🏷️", str(pat)) for pat, name in s["rules"].items()]
        return extra + CATEGORIES

    def categorize(self, merchant, recurring, s):
        m = (merchant or "").upper()
        for name, icon, pat in self._rules(s):
            try:
                if re.search(pat, m, re.I):
                    return name, icon
            except re.error:
                continue
        if m in recurring:
            return "Subscriptions", "🔁"
        return OTHER

    @staticmethod
    def _recurring(txns):
        """Merchants charged in 2+ different months for about the same amount look like subscriptions."""
        seen = {}
        for t in txns:
            d = dt.datetime.fromtimestamp(t["t"])
            seen.setdefault(t["merchant"].upper(), []).append((_month_key(d), t["amount"]))
        out = set()
        for m, xs in seen.items():
            # any two charges in different months within ~15% of each other
            if any(k1 != k2 and abs(a1 - a2) <= max(a1, a2) * 0.15 + 0.5
                   for i, (k1, a1) in enumerate(xs) for k2, a2 in xs[i + 1:]):
                out.add(m)
        return out

    # ---- reading the alert emails
    def fetch(self):
        """Called by the helper's Cached wrapper (background thread)."""
        if not gsync.has_scope(gsync.GMAIL_SCOPE):
            return {"state": "needs-gmail" if gsync.connected() else "off"}
        s = self.settings()
        since = max(self.cache["since"], time.time() - DAYS * 86400)
        ids = gsync.search_messages(f"{s['query']} after:{int(since)}", limit=400)
        parsed = self.cache["parsed"]
        new = [i for i in ids if i not in parsed]
        for msg_id in new[:150]:  # catch up in chunks
            try:
                parsed[msg_id] = parse(*gsync.message_text(msg_id))
            except Exception:
                logging.exception("couldn't read alert %s", msg_id)
        cut = time.time() - 400 * 86400
        self.cache["parsed"] = {k: v for k, v in parsed.items() if v is None or v["t"] > cut}
        data = self.summary(s, more=len(new) > 150)
        if s["remind"]:
            try:
                self._remind(data)
            except Exception:
                logging.exception("dues reminder failed")
            data = self.summary(s, more=len(new) > 150)
        self._write()
        return data

    def _txns(self, s):
        txns = [t for t in self.cache["parsed"].values() if t and t["t"] >= self.cache["since"]]
        cards = sorted({t["card"] for t in txns if t["card"]})
        if s["card"]:
            txns = [t for t in txns if t["card"] == s["card"]]
        txns.sort(key=lambda t: t["t"], reverse=True)
        return txns, cards

    # ---- dues: what to pay off, and when
    def dues(self, s=None, txns=None):
        s = s or self.settings()
        if txns is None:
            txns, _ = self._txns(s)
        today = dt.date.today()
        start = dt.date.fromtimestamp(self.cache["since"])
        totals = {}
        for t in txns:
            if (t["currency"] or s["currency"]) == s["currency"]:
                k = _month_key(dt.datetime.fromtimestamp(t["t"]))
                totals[k] = totals.get(k, 0) + t["amount"]
        out = []
        y, m = start.year, start.month
        while (y, m) <= (today.year, today.month):
            key = f"{y:04d}-{m:02d}"
            py, pm = (y + (m + s["lag"] - 1) // 12, (m + s["lag"] - 1) % 12 + 1)
            due = _last_day(py, pm)
            month_over = (y, m) < (today.year, today.month)
            if totals.get(key) and (month_over or s["lag"] == 0):
                paid = self.cache["paid"].get(key)
                out.append({"month": key, "name": _month_name(key), "amount": round(totals[key], 2),
                            "due": due.isoformat(), "paid": paid,
                            "state": "paid" if paid else "overdue" if today > due else "due" if today == due else "upcoming"})
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    def _remind(self, data):
        """On the due day, put 'Pay off the card' on the to-do list (Google Tasks) once per month."""
        if not gsync.connected():
            return
        for d in data["dues"]:
            if d["state"] in ("due", "overdue") and d["month"] not in self.cache["tasks"]:
                c = data["currency"]
                card = f" ending {data['card']}" if data["card"] else ""
                res = gsync.api("POST", gsync._tasks_url(self.tasks_list()), {
                    "title": f"Pay off card{card}: {d['name']} dues {c} {d['amount']:,.2f}",
                    "notes": "Added by the home screen. Mark it paid on the Spending widget.",
                    "due": f"{d['due']}T00:00:00.000Z"})
                self.cache["tasks"][d["month"]] = res.get("id", "")
                logging.info("dues reminder added for %s", d["month"])

    def mark_paid(self, month, paid=True):
        month = str(month)
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return False
        if paid:
            self.cache["paid"][month] = time.time()
        else:
            self.cache["paid"].pop(month, None)
        task = self.cache["tasks"].get(month)
        if task and gsync.connected():
            try:
                gsync.api("PATCH", gsync._tasks_url(self.tasks_list(), task),
                          {"status": "completed"} if paid else {"status": "needsAction", "completed": None})
            except Exception:
                logging.exception("couldn't update the dues task")
        self._write()
        return True

    def summary(self, s=None, more=False):
        s = s or self.settings()
        txns, cards = self._txns(s)
        recurring = self._recurring(txns)
        for t in txns:
            t["cat"], t["icon"] = self.categorize(t["merchant"], recurring, s)
        now = dt.datetime.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_start = (month_start - dt.timedelta(days=1)).replace(day=1)
        same_point_prev = prev_start + (now - month_start)
        home = lambda t: (t["currency"] or s["currency"]) == s["currency"]
        this_month = [t for t in txns if t["t"] >= month_start.timestamp()]
        prev_month = [t for t in txns if prev_start.timestamp() <= t["t"] < month_start.timestamp()]
        daily = [0.0] * now.day
        for t in this_month:
            if home(t):
                daily[dt.datetime.fromtimestamp(t["t"]).day - 1] += t["amount"]
        merchants, cats = {}, {}
        for t in this_month:
            if home(t):
                merchants[t["merchant"]] = merchants.get(t["merchant"], 0) + t["amount"]
                c = cats.setdefault(t["cat"], {"name": t["cat"], "icon": t["icon"], "total": 0.0, "count": 0})
                c["total"] += t["amount"]
                c["count"] += 1
        for c in cats.values():
            c["total"] = round(c["total"], 2)
        return {
            "state": "ok", "currency": s["currency"], "card": s["card"], "cards": cards, "budget": s["budget"],
            "since": self.cache["since"],
            "month": round(sum(t["amount"] for t in this_month if home(t)), 2),
            "prevSamePoint": round(sum(t["amount"] for t in prev_month if home(t) and t["t"] < same_point_prev.timestamp()), 2),
            "prevMonth": round(sum(t["amount"] for t in prev_month if home(t)), 2),
            "count": len(this_month), "daily": [round(x, 2) for x in daily],
            "top": sorted(([k, round(v, 2)] for k, v in merchants.items()), key=lambda kv: -kv[1])[:4],
            "categories": sorted(cats.values(), key=lambda c: -c["total"]),
            "recent": txns[:8], "catchingUp": more,
            "foreign": sum(1 for t in this_month if not home(t)),
            "dues": self.dues(s, txns),
        }

    def describe(self, data):
        if not data or data.get("state") != "ok":
            return "Spending isn't available yet (Gmail permission needed for the card alert emails)."
        c = data["currency"]
        since = dt.datetime.fromtimestamp(data["since"]).strftime("%d %b %Y")
        lines = [f"Card spending (tracked since {since}) this month: {c} {data['month']:.2f} over {data['count']} transactions"
                 f" (same point last month: {c} {data['prevSamePoint']:.2f}; all of last month {c} {data['prevMonth']:.2f})."]
        if data["budget"]:
            lines.append(f"Monthly budget {c} {data['budget']:.0f}: {100 * data['month'] / data['budget']:.0f}% used.")
        if data["categories"]:
            lines.append("By category this month: " + ", ".join(
                f"{x['name']} {c} {x['total']:.2f} ({x['count']})" for x in data["categories"]))
        for d in data["dues"]:
            lines.append(f"Card dues for {d['name']}: {c} {d['amount']:.2f}, pay by {d['due']}: "
                         + ("PAID" if d["paid"] else d["state"].upper()))
        for t in data["recent"]:
            when = dt.datetime.fromtimestamp(t["t"]).strftime("%a %d %b %H:%M")
            lines.append(f"- {when}: {t['currency']} {t['amount']:.2f} at {t['merchant']} [{t['cat']}]")
        return "\n".join(lines)


if __name__ == "__main__":
    sample = """Transaction Ref: 123
Dear Sir / Madam,
We refer to your card transaction request dated 23/11/26.
Date & Time: 23 NOV 14:05
Amount: USD12.50
From: BANK card ending 1234
To: GRAB* A-ABCDEFG
If unauthorised, call 24/7 Fraud Hotline now"""
    import time
    print(parse(time.time(), "Card Transaction Alert", sample))
