"""Internet access for the assistant: a web search and a page reader.

Only used after the user clicks Allow on the wallpaper. Search goes through
DuckDuckGo's plain-HTML "lite" page (no API key, no account), with Wikipedia's
API as the fallback (and Brave's official search API first, if a key is set). Ads and off-topic results are dropped, and the top pages
are downloaded and reduced to readable text so the local model can use them.
"""
import datetime as dt
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
MAX_PAGE_BYTES = 3_000_000
MAX_TEXT = 7000


def _get(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_PAGE_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        return r.geturl(), r.headers.get_content_type(), raw.decode(charset, "replace")


def _clean(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", s)).split())


STOP = set("""a an the of in on at to for and or is are was were be been being what whats who whos whom which when where why how
do does did can could should would will i me my you your it its this that these those as by with from about into than
current currently latest now today right present 2024 2025 2026 2027 please tell find search web look up show give know""".split())
JUNK_HOSTS = ("duckduckgo.com", "bing.com", "doubleclick.net", "googleadservices.com", "google.com/aclk", "amazon-adsystem")
ADULT = re.compile(r"\b(porn|xxx|sex videos?|nude|milf|hentai|onlyfans)\b", re.I)
TRUSTED = ("wikipedia.org", ".gov", ".gov.sg", ".gc.ca", ".edu", "britannica.com", "reuters.com", "apnews.com", "bbc.",
           "who.int", "un.org", "mayoclinic.org", "nih.gov", "github.com", "python.org", "microsoft.com", "mozilla.org")


def _words(s):
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1]


def _score(query_words, r):
    """How well a result matches the query (0 = unrelated). Trusted reference sites get a small boost."""
    hay = f"{r['title']} {r['snippet']} {r['url']}".lower()
    hits = sum(1 for w in set(query_words) if w in hay)
    if not hits:
        return 0
    host = urllib.parse.urlparse(r["url"]).hostname or ""
    return hits / max(1, len(set(query_words))) + (0.3 if any(t in host for t in TRUSTED) else 0)


def _ok(r):
    url = r["url"]
    if not url.startswith(("http://", "https://")):
        return False
    host = urllib.parse.urlparse(url).hostname or ""
    if any(j in host or j in url for j in JUNK_HOSTS) or "/aclick" in url or "y.js" in url:
        return False
    return not ADULT.search(f"{r['title']} {r['snippet']} {host}")


def _ddg(query, limit):
    """DuckDuckGo's plain-HTML lite page (POST, like the real form). Sponsored rows are dropped."""
    _, _, page = _get("https://lite.duckduckgo.com/lite/", data=urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode())
    if "anomaly" in page[:5000].lower() and "result-link" not in page:
        raise OSError("DuckDuckGo asked for a captcha")
    out = []
    links = list(re.finditer(r"<a[^>]+href=\"([^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>", page, re.S))
    for i, m in enumerate(links):
        end = links[i + 1].start() if i + 1 < len(links) else len(page)
        chunk = page[m.end():end]
        if "Sponsored link" in chunk[:400] or "result-sponsored" in chunk[:1200] or "y.js" in m.group(1):
            continue  # an ad
        href = html.unescape(m.group(1))
        target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        if target.startswith("//"):
            target = "https:" + target
        snip = re.search(r"class='result-snippet'>(.*?)</td>", chunk, re.S)
        out.append({"title": _clean(m.group(2)), "url": target, "snippet": _clean(snip.group(1)) if snip else ""})
    return out[:limit * 2]


def _wikipedia(query, limit):
    _, _, page = _get("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": limit}))
    return [{"title": x["title"] + " - Wikipedia",
             "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(x["title"].replace(" ", "_")),
             "snippet": _clean(x.get("snippet", ""))} for x in json.loads(page)["query"]["search"]]


def _brave(query, limit, key):
    """Brave Search's official API (optional: free key from api-dashboard.search.brave.com, set web.brave_key)."""
    req = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": min(20, limit * 2)}), headers={"Accept": "application/json", "X-Subscription-Token": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [{"title": _clean(x.get("title", "")), "url": x.get("url", ""), "snippet": _clean(x.get("description", ""))}
            for x in (data.get("web") or {}).get("results", [])]


_CACHE = {}  # query -> (time, results, note); avoids repeat requests that trip DuckDuckGo's rate limit
_last_ddg = [0.0]


def _ddg_polite(query, limit):
    wait = 2.5 - (time.time() - _last_ddg[0])
    if wait > 0:
        time.sleep(wait)
    _last_ddg[0] = time.time()
    return _ddg(query, limit)


def search(query, limit=8, brave_key=""):
    """Returns ([{title, url, snippet}] best first, note). Engines: Brave API (if a key is set), DuckDuckGo,
    then Wikipedia. Ads, adult pages and results that miss most of the query's key words are dropped."""
    query = " ".join(str(query).split())[:300]
    if not query:
        raise ValueError("empty search")
    hit = _CACHE.get(query.lower())
    if hit and time.time() - hit[0] < 600:
        return hit[1], hit[2]
    qw = list(dict.fromkeys(_words(query) or query.lower().split()))
    need = 1 if len(qw) <= 2 else max(2, (len(qw) + 1) // 2)  # most of the key words must appear
    engines = ([("brave", lambda q, n: _brave(q, n, brave_key))] if brave_key else []) + \
        [("duckduckgo", _ddg_polite), ("wikipedia", _wikipedia)]
    seen, ranked, failed = set(), [], []
    for name, engine in engines:
        try:
            found = engine(query, limit)
        except (OSError, ValueError, KeyError) as exc:
            logging.info("search: %s failed: %s", name, exc)
            failed.append(name)
            continue
        for pos, r in enumerate(found):
            key = r["url"].split("#")[0].rstrip("/").lower()
            if key in seen or not _ok(r):
                continue
            hay = f"{r['title']} {r['snippet']} {r['url']}".lower()
            if sum(1 for w in qw if w in hay) < need:
                continue
            seen.add(key)
            ranked.append((_score(qw, r) - pos * 0.02, r))
        if len(ranked) >= 4:
            break  # good enough; don't hit the other engines
    ranked.sort(key=lambda x: -x[0])
    note = ""
    if "duckduckgo" in failed and not brave_key:
        note = "(DuckDuckGo is limiting searches from this PC right now, so these results come from Wikipedia only.)"
    out = [r for _, r in ranked[:limit]]
    if out:
        _CACHE[query.lower()] = (time.time(), out, note)
    return out, note


def _passage(text, query_words, size=1400):
    """The part of a page that best matches the query."""
    paras = [p for p in re.split(r"\n+", text) if len(p) > 40]
    if not paras:
        return text[:size]
    best = max(range(len(paras)), key=lambda i: sum(paras[i].lower().count(w) for w in set(query_words)) - i * 0.01)
    out, i = "", best
    while i < len(paras) and len(out) < size:
        out += paras[i] + "\n"
        i += 1
    if best > 0 and len(out) < size:
        out = paras[best - 1] + "\n" + out
    return out[:size].strip()


def search_text(query, read=2, brave_key=""):
    """Search results as text for the model, plus the most relevant passage from the top pages."""
    results, note = search(query, brave_key=brave_key)
    if not results:
        return (f'No relevant results for "{query}". {note} Try again with different, more specific words '
                "(names, places, the exact product or event).").replace("  ", " ")
    qw = _words(query)
    lines = [f'Web results for "{query}" (searched {dt.date.today():%d %B %Y}). '
             "These are current: prefer them over your own memory." + (f" {note}" if note else "")]
    for i, r in enumerate(results, 1):
        lines.append(f"\n{i}. {r['title']}\n{r['url']}\n{r['snippet']}")
    pages = []
    with ThreadPoolExecutor(max_workers=read) as pool:
        jobs = [(r, pool.submit(_page_text, r["url"])) for r in results[:read]]
        for r, job in jobs:
            try:
                title, text = job.result(timeout=15)
            except Exception:
                continue
            if text:
                pages.append(f"\n--- From {r['url']} ---\n{_passage(text, qw)}")
    if pages:
        lines.append("\nExcerpts from the top results:" + "".join(pages))
    return "\n".join(lines)


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("## ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def _public(url):
    url = str(url).strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith(("10.", "192.168.", "169.254.")) or host.endswith(".local"):
        raise ValueError("only public web pages can be opened")
    return url


def _page_text(url, timeout=12):
    """(title, readable text) of a public page; text is empty for files that aren't text."""
    final, ctype, body = _get(_public(url), timeout=timeout)
    if "html" not in ctype and not ctype.startswith("text/"):
        return final, ""
    if "html" in ctype:
        p = _Text()
        p.feed(body)
        text, title = "".join(p.parts), " ".join(p.title.split())
    else:
        text, title = body, ""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if len(line) > 1)
    return title, re.sub(r"\n{3,}", "\n\n", text)


def read_page(url):
    url = _public(url)
    title, text = _page_text(url, timeout=20)
    if not text:
        return f"{url}\n(This file can't be read as text.)"
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n…(page shortened)"
    return f"{title}\n{url}\n\n{text}".strip()


if __name__ == "__main__":
    import sys
    print(search_text(" ".join(sys.argv[1:]) or "open-meteo api"))
