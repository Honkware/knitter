#!/usr/bin/env python3
"""knitter: polite, zero-dependency Nitter RSS reader with SQLite archive."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass

USER_AGENT = "knitter/0.6 (+polite-rss-reader)"
STATUS_API = "https://status.d420.de/api/v1/instances"
DEFAULT_DB = pathlib.Path(
    os.environ.get("KNITTER_DB", pathlib.Path.home() / ".cache" / "knitter" / "knitter.db")
)
FALLBACK_INSTANCES = (
    "https://nitter.mie.foundation",
    "https://nitter.mizuki.st",
)

INSTANCES_TTL = 3600
FEED_TTL = 900
COOLDOWN_SEC = 3600
REQUEST_DELAY = float(os.environ.get("KNITTER_DELAY", "1.5"))
HTTP_TIMEOUT = 8.0
MAX_SEEDS = 6

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r'\(|\)|"[^"]+"|[^\s()]+')


@dataclass
class Post:
    author: str
    published: str
    text: str
    url: str
    source: str
    is_new: bool = False


class Store:
    """Single SQLite store for archived posts, ETag cache, instances, and cooldowns."""

    def __init__(self, path: pathlib.Path = DEFAULT_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feeds (
                k TEXT PRIMARY KEY,
                inst TEXT NOT NULL,
                etag TEXT,
                lm TEXT,
                xml TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posts (
                url TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                published TEXT NOT NULL,
                text TEXT NOT NULL,
                source TEXT NOT NULL,
                first_seen REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_posts_seen ON posts(first_seen DESC);
            """
        )

    def get_kv(self, key: str, max_age: float | None = None):
        row = self.db.execute("SELECT v, ts FROM kv WHERE k = ?", (key,)).fetchone()
        if not row or (max_age is not None and time.time() - row[1] >= max_age):
            return None
        return json.loads(row[0])

    def set_kv(self, key: str, val) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO kv VALUES (?, ?, ?)",
            (key, json.dumps(val), time.time()),
        )
        self.db.commit()

    def get_feed(self, key: str):
        return self.db.execute(
            "SELECT inst, etag, lm, xml, ts FROM feeds WHERE k = ?", (key,)
        ).fetchone()

    def set_feed(
        self, key: str, inst: str, etag: str | None, lm: str | None, xml: str
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO feeds VALUES (?, ?, ?, ?, ?, ?)",
            (key, inst, etag, lm, xml, time.time()),
        )
        self.db.commit()

    def save_posts(self, posts: list[Post]) -> list[Post]:
        now = time.time()
        with self.db:
            for post in posts:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                    (post.url, post.author, post.published, post.text, post.source, now),
                )
                post.is_new = cur.rowcount == 1
        return posts

    def all_posts(self, limit: int = 500) -> list[Post]:
        rows = self.db.execute(
            "SELECT author, published, text, url, source "
            "FROM posts ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [Post(*row) for row in rows]


def _unique(*seqs: list[str] | tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(u.rstrip("/") for seq in seqs for u in seq if u))


def _http_get(
    url: str,
    timeout: float = HTTP_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, str]]:
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers.items())


def discover_instances(store: Store) -> list[str]:
    """Return healthy RSS instances from status.d420.de ranked by score and latency."""
    if cached := store.get_kv("instances", max_age=INSTANCES_TTL):
        return _unique(cached, FALLBACK_INSTANCES)

    try:
        raw, _ = _http_get(STATUS_API, headers={"Accept": "application/json"})
        hosts = [
            h
            for h in json.loads(raw).get("hosts", [])
            if h.get("healthy")
            and h.get("rss")
            and not h.get("is_bad_host")
            and isinstance(h.get("url"), str)
            and h["url"].startswith("https://")
        ]
        hosts.sort(key=lambda h: (-int(h.get("points") or 0), int(h.get("ping_avg") or 99999)))
        if urls := [h["url"].rstrip("/") for h in hosts]:
            store.set_kv("instances", urls)
            return _unique(urls, FALLBACK_INSTANCES)
    except Exception:
        pass

    return list(FALLBACK_INSTANCES)


def _parse_rss(xml_bytes: bytes, instance: str, limit: int) -> list[Post]:
    inst_host = urllib.parse.urlparse(instance).netloc
    posts: list[Post] = []

    for item in ET.fromstring(xml_bytes).findall(".//item")[:limit]:
        author = (
            item.findtext("{http://purl.org/dc/elements/1.1/}creator")
            or item.findtext("author")
            or ""
        ).strip()
        title = (item.findtext("title") or "").strip()
        raw_desc = item.findtext("description") or ""
        desc = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw_desc))).strip()
        published = (item.findtext("pubDate") or "").strip()

        link = (item.findtext("link") or "").strip()
        parsed = urllib.parse.urlparse(link)
        url = f"https://x.com{parsed.path}" if parsed.netloc == inst_host else link

        body = desc if len(desc) >= len(title) else title
        posts.append(Post(author, published, body, url, instance))

    return posts


class RSSClient:
    """Polite RSS client honoring robots.txt, ETags, TTL cache, and backoff."""

    def __init__(self, store: Store, instances: list[str] | None = None) -> None:
        self.store = store
        self.instances = _unique(instances) if instances else discover_instances(store)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_net = 0.0

    def _cooldown(self, base: str, seconds: float = COOLDOWN_SEC) -> None:
        self.store.set_kv(f"cd:{base}", time.time() + max(60.0, seconds))

    def _robots_check(self, base: str, url: str) -> tuple[bool, float]:
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                raw, _ = _http_get(f"{base}/robots.txt", timeout=5.0)
                rp.parse(raw.decode("utf-8", errors="replace").splitlines())
                self._robots[base] = rp
            except urllib.error.HTTPError as exc:
                self._robots[base] = None
                if exc.code in (401, 403, 429):
                    self._cooldown(base)
                    return False, 0.0
            except Exception:
                self._robots[base] = None

        rp = self._robots.get(base)
        if rp is None:
            return True, 0.0

        allowed = rp.can_fetch(USER_AGENT, url) and rp.can_fetch("*", url)
        delay = float(rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*") or 0.0)
        return allowed, delay

    def _pace(self, extra_delay: float = 0.0) -> None:
        wait = max(REQUEST_DELAY, extra_delay)
        if self._last_net > 0:
            remaining = wait - (time.time() - self._last_net)
            if remaining > 0:
                time.sleep(remaining)
        self._last_net = time.time()

    def fetch(self, path: str, limit: int = 15) -> list[Post]:
        key = hashlib.sha256(path.encode()).hexdigest()[:24]
        cached = self.store.get_feed(key)
        if cached and (time.time() - cached[4]) < FEED_TTL:
            return self.store.save_posts(_parse_rss(cached[3].encode(), cached[0], limit))

        errors: list[str] = []
        for base in self.instances:
            until = self.store.get_kv(f"cd:{base}")
            if until and time.time() < float(until):
                errors.append(f"{base}: cooldown")
                continue

            url = f"{base}{path}"
            allowed, crawl_delay = self._robots_check(base, url)
            if not allowed:
                errors.append(f"{base}: disallowed by robots.txt")
                continue

            headers = {"Accept": "application/rss+xml, application/xml, text/xml"}
            if cached and cached[0] == base:
                if cached[1]:
                    headers["If-None-Match"] = cached[1]
                if cached[2]:
                    headers["If-Modified-Since"] = cached[2]

            self._pace(crawl_delay)
            try:
                raw, resp_hdrs = _http_get(url, headers=headers)
                head = raw[:512].lower()
                if b"<rss" not in head and b"<feed" not in head:
                    self._cooldown(base)
                    errors.append(f"{base}: non-RSS response")
                    continue

                posts = self.store.save_posts(_parse_rss(raw, base, limit))
                self.store.set_feed(
                    key,
                    base,
                    resp_hdrs.get("ETag"),
                    resp_hdrs.get("Last-Modified"),
                    raw.decode("utf-8", errors="replace"),
                )
                return posts
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and cached:
                    self.store.set_feed(key, base, cached[1], cached[2], cached[3])
                    return self.store.save_posts(_parse_rss(cached[3].encode(), base, limit))
                if exc.code == 404:
                    self.store.set_feed(key, base, None, None, "<rss><channel></channel></rss>")
                    return []
                if exc.code in (401, 403, 429):
                    ra = exc.headers.get("Retry-After") if exc.headers else None
                    cd = float(ra) if (ra and ra.isdigit()) else COOLDOWN_SEC
                    self._cooldown(base, cd)
                errors.append(f"{base}: HTTP {exc.code}")
            except Exception as exc:
                errors.append(f"{base}: {exc}")

        raise RuntimeError("no open RSS instance succeeded: " + " | ".join(errors))

    def search(self, query: str, limit: int = 15) -> list[Post]:
        encoded = urllib.parse.quote(query.strip(), safe="")
        return self.fetch(f"/search/rss?f=tweets&q={encoded}", limit)

    def user(self, handle: str, limit: int = 15) -> list[Post]:
        clean = urllib.parse.quote(handle.lstrip("@").strip())
        return self.fetch(f"/{clean}/rss", limit)


# -- Universal boolean and phrase query planner --


def _specificity(seed: str) -> float:
    raw = seed.strip('"')
    score = float(len(raw))
    if any(ch.isdigit() for ch in raw):
        score += 4.0
    if any(ch in raw for ch in "._-/#@"):
        score += 3.0
    if any(ch.isupper() for ch in raw):
        score += 3.0 + (2.0 if any(ch.isupper() for ch in raw[1:]) else 0.0)
    if seed.startswith('"') and " " in raw:
        score += 3.0
    return score


def parse_expr(expr: str) -> tuple:
    tokens = _TOKEN_RE.findall(expr)
    if not tokens:
        raise ValueError("empty query")
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def pop() -> str:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def parse_or() -> tuple:
        node = parse_and()
        while (peek() or "").upper() == "OR":
            pop()
            node = ("OR", node, parse_and())
        return node

    def parse_and() -> tuple:
        node = parse_not()
        while peek() not in (None, ")") and (peek() or "").upper() != "OR":
            if (peek() or "").upper() == "AND":
                pop()
            node = ("AND", node, parse_not())
        return node

    def parse_not() -> tuple:
        tok = peek()
        if tok is None:
            raise ValueError("unexpected end of query")
        if tok.upper() == "NOT":
            pop()
            return ("NOT", parse_not())
        if tok.startswith("-") and len(tok) > 1:
            pop()
            term = tok[1:].strip('"')
            return ("NOT", ("TERM", term.lower(), term, False))

        tok = pop()
        if tok == "(":
            inner = parse_or()
            if peek() != ")":
                raise ValueError("missing ')'")
            pop()
            return inner

        quoted = len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"'
        raw = tok[1:-1] if quoted else tok
        return ("TERM", raw.lower(), raw, quoted)

    root = parse_or()
    if peek() is not None:
        raise ValueError(f"unexpected token {peek()!r}")
    return root


def plan_seeds(node: tuple) -> list[str]:
    op = node[0]
    if op == "TERM":
        return [f'"{node[2]}"'] if (node[3] or " " in node[2]) else [node[2]]
    if op == "NOT":
        return []
    if op == "OR":
        return _unique(plan_seeds(node[1]), plan_seeds(node[2]))

    left, right = plan_seeds(node[1]), plan_seeds(node[2])
    if not left or not right:
        return left or right

    def rank(seeds: list[str]) -> tuple[int, float]:
        return (len(seeds), -(sum(_specificity(s) for s in seeds) / len(seeds)))

    return left if rank(left) <= rank(right) else right


def eval_expr(node: tuple, low_text: str) -> bool:
    op = node[0]
    if op == "TERM":
        return node[1] in low_text
    if op == "NOT":
        return not eval_expr(node[1], low_text)
    if op == "AND":
        return eval_expr(node[1], low_text) and eval_expr(node[2], low_text)
    return eval_expr(node[1], low_text) or eval_expr(node[2], low_text)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="knitter",
        description="Polite Nitter RSS reader with SQLite archive and boolean filtering.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="",
        help='Search term or boolean expression, e.g. \'(python OR rust) AND ("release notes" OR launch)\'',
    )
    parser.add_argument("-u", "--user", action="append", default=[], help="Account handle (repeatable)")
    parser.add_argument("-i", "--instance", action="append", default=[], help="Custom instance URL (repeatable)")
    parser.add_argument("-n", "--limit", type=int, default=15, help="Max posts per feed (default: 15)")
    parser.add_argument("--new", action="store_true", help="Only show posts not seen before")
    parser.add_argument("--local", action="store_true", help="Search local SQLite archive only")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    store = Store(DEFAULT_DB)
    ast = parse_expr(args.query) if args.query else None

    if args.local:
        posts = store.all_posts(max(args.limit, 500))
        if ast:
            posts = [p for p in posts if eval_expr(ast, f"{p.author} {p.text}".lower())]
        posts = posts[: args.limit]
    else:
        if not args.query and not args.user:
            parser.error("provide a query, --user, or --local")

        client = RSSClient(store, args.instance or None)
        seeds = plan_seeds(ast)[:MAX_SEEDS] if (ast and not args.user) else []

        by_url: dict[str, Post] = {}
        for seed in seeds:
            for post in client.search(seed, args.limit):
                by_url.setdefault(post.url, post)
        for handle in args.user:
            for post in client.user(handle, args.limit):
                by_url.setdefault(post.url, post)

        posts = list(by_url.values())
        if ast:
            posts = [p for p in posts if eval_expr(ast, f"{p.author} {p.text}".lower())]
        if args.new:
            posts = [p for p in posts if p.is_new]

    if args.json:
        print(json.dumps([asdict(p) for p in posts], indent=2))
    else:
        for post in posts:
            prefix = "[NEW] " if post.is_new else ""
            print(f"{prefix}[{post.published}] {post.author}: {post.text}\n  {post.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
