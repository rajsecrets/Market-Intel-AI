import http.server
import json
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
import threading
import os
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

PORT          = int(os.environ.get("PORT", 7432))
NEWS_API_KEY  = os.environ.get("NEWS_API_KEY",  "c2a20131d4674a498dc85d945e4942e5")
GNEWS_KEY     = os.environ.get("GNEWS_KEY",     "1e8ef318edc4bf804d0b6331af32f8b1")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "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")

IST = timezone(timedelta(hours=5, minutes=30))

RSS_FEEDS = [
    # Feedburner (Google CDN) — always open
    ("https://feeds.feedburner.com/ndtvprofit-latest",                                                                        "NDTV Profit"),
    # Google News RSS — 4 targeted queries for full coverage
    ("https://news.google.com/rss/search?q=india+stock+market+RBI+SEBI+Nifty+Sensex&hl=en-IN&gl=IN&ceid=IN:en",             "Google News: Markets"),
    ("https://news.google.com/rss/search?q=india+quarterly+results+earnings+BSE+NSE&hl=en-IN&gl=IN&ceid=IN:en",             "Google News: Earnings"),
    ("https://news.google.com/rss/search?q=India+economy+inflation+GDP+RBI+interest+rate&hl=en-IN&gl=IN&ceid=IN:en",        "Google News: Macro"),
    ("https://news.google.com/rss/search?q=Sensex+Nifty+FII+DII+Indian+market+trading&hl=en-IN&gl=IN&ceid=IN:en",           "Google News: Trading"),
    # Open RSS — no auth, no crawler blocking
    ("https://www.livemint.com/rss/markets",                                                                                  "LiveMint"),
    ("https://www.thehindubusinessline.com/feeder/default.rss",                                                              "BusinessLine"),
]

BROWSER_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/rss+xml, application/xml, text/xml, text/html, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control":   "no-cache",
    "Referer":         "https://www.google.com/",
}


# ── Date normalisation ────────────────────────────────────────────────────────

def normalise_date(raw):
    """Parse RSS/ISO date → ISO-8601 in IST. Clamps future timestamps to now."""
    if not raw:
        return ""
    now_ist = datetime.now(IST)
    try:
        dt = parsedate_to_datetime(raw)          # RFC-2822 (RSS pubDate)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # ISO-8601
        except Exception:
            return raw
    dt_ist = dt.astimezone(IST)
    if dt_ist > now_ist:
        dt_ist = now_ist
    return dt_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_url(url, timeout=12):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        # detect encoding from Content-Type header
        ct = r.headers.get_content_charset() or "utf-8"
        return raw.decode(ct, errors="replace")


def fetch_newsapi():
    q = urllib.parse.quote(
        "India stocks OR RBI OR SEBI OR Nifty OR Sensex OR \"India economy\" OR \"crude oil\" OR \"Fed rate\" OR inflation"
    )
    url = (
        f"https://newsapi.org/v2/everything?q={q}"
        f"&language=en&sortBy=publishedAt&pageSize=30&apiKey={NEWS_API_KEY}"
    )
    try:
        data = json.loads(fetch_url(url))
        if data.get("status") == "error":
            return [], data.get("message", "unknown error")
        arts = [
            {
                "title":       a["title"],
                "url":         a["url"],
                "source":      a["source"]["name"],
                "publishedAt": normalise_date(a["publishedAt"]),
                "description": (a.get("description") or ""),
            }
            for a in data.get("articles", [])
            if a.get("title") and "[Removed]" not in a.get("title", "")
        ]
        return arts, None
    except Exception as e:
        return [], str(e)


def fetch_gnews():
    q = urllib.parse.quote("india stock market RBI Nifty Sensex economy")
    url = f"https://gnews.io/api/v4/search?q={q}&lang=en&country=in&max=20&token={GNEWS_KEY}"
    try:
        data = json.loads(fetch_url(url))
        if "errors" in data:
            return [], str(data["errors"])
        arts = [
            {
                "title":       a["title"],
                "url":         a["url"],
                "source":      a["source"]["name"],
                "publishedAt": normalise_date(a["publishedAt"]),
                "description": (a.get("description") or ""),
            }
            for a in data.get("articles", [])
        ]
        return arts, None
    except Exception as e:
        return [], str(e)


def fetch_rss(url, source_name):
    try:
        raw = fetch_url(url)
        # Strip leading BOM / whitespace that trips the XML parser
        raw = raw.lstrip("\ufeff").strip()
        # If the feed returned an HTML error page, bail early
        if raw[:100].lstrip().startswith("<!"):
            return [], "blocked (HTML response)"
        root = ET.fromstring(raw)
        result = []
        for item in root.findall(".//item")[:15]:
            title = (item.findtext("title") or "").replace("<![CDATA[", "").replace("]]>", "").strip()
            link  = item.findtext("link") or "#"
            desc  = (item.findtext("description") or "").replace("<![CDATA[", "").replace("]]>", "")
            desc  = re.sub(r"<[^>]+>", "", desc).strip()[:200]
            pub   = item.findtext("pubDate") or ""
            if title:
                result.append({
                    "title":       title,
                    "url":         link,
                    "source":      source_name,
                    "publishedAt": normalise_date(pub),
                    "description": desc,
                })
        return result, None
    except ET.ParseError as e:
        return [], f"XML parse error: {e}"
    except Exception as e:
        return [], str(e)


def collect_all_news():
    results = {"sources": {}, "articles": []}
    seen = set()

    def add(arts, name, err):
        results["sources"][name] = {"count": len(arts), "error": err}
        for a in arts:
            key = a["title"][:60].lower().replace(" ", "")
            if key not in seen:
                seen.add(key)
                results["articles"].append(a)

    arts, err = fetch_newsapi()
    add(arts, "NewsAPI", err)

    arts, err = fetch_gnews()
    add(arts, "GNews", err)

    for feed_url, name in RSS_FEEDS:
        arts, err = fetch_rss(feed_url, name)
        add(arts, name, err)

    return results


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {args[0]} {args[1]}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-api-key, anthropic-version")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, mime):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "india-market-intel.html")
            self.send_file(html, "text/html; charset=utf-8")
        elif self.path == "/api/news":
            print("  Fetching all sources…")
            data = collect_all_news()
            print(f"  Done — {len(data['articles'])} articles")
            self.send_json(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/anthropic":
            length  = int(self.headers.get("Content-Length", 0))
            body    = self.rfile.read(length)
            try:
                payload = json.loads(body)
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps(payload).encode(),
                    headers={
                        "Content-Type":      "application/json",
                        "x-api-key":         ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=90) as r:
                    self.send_json(json.loads(r.read().decode()))
            except urllib.error.HTTPError as e:
                self.send_json(json.loads(e.read().decode()), e.code)
            except Exception as e:
                self.send_json({"error": {"message": str(e)}}, 500)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    bind = "localhost" if PORT == 7432 else "0.0.0.0"
    server = http.server.ThreadingHTTPServer((bind, PORT), Handler)
    print(f"\n  India Market Intel  →  http://{bind}:{PORT}\n")
    if bind == "localhost":
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
