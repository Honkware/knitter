"""Offline unit tests for knitter."""
from __future__ import annotations

import io
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

import knitter

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <item>
      <title>Short title</title>
      <dc:creator>@maren</dc:creator>
      <description>&lt;p&gt;Two more weeks until the release notes are finalized.&lt;/p&gt;</description>
      <pubDate>Tue, 24 Nov 2026 15:00:00 GMT</pubDate>
      <link>http://nitter.example.org/maren/status/12344#m</link>
    </item>
    <item>
      <title>Second post</title>
      <dc:creator>@tomas</dc:creator>
      <description>&lt;p&gt;Saved the thread locally before it scrolled off page ten.&lt;/p&gt;</description>
      <pubDate>Tue, 24 Nov 2026 16:00:00 GMT</pubDate>
      <link>https://nitter.example.org/tomas/status/67890#m</link>
    </item>
  </channel>
</rss>
"""


class KnitterTests(unittest.TestCase):
    def test_parse_rss_normalizes_links_and_strips_html(self) -> None:
        posts = knitter._parse_rss(SAMPLE_RSS, "https://nitter.example.org", limit=10)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].author, "@maren")
        self.assertEqual(posts[0].url, "https://x.com/maren/status/12344")
        self.assertEqual(
            posts[0].text,
            "Two more weeks until the release notes are finalized.",
        )
        self.assertEqual(posts[1].url, "https://x.com/tomas/status/67890")

    def test_boolean_parser_and_evaluator(self) -> None:
        ast = knitter.parse_expr('(python OR rust) AND ("release notes" OR launch) -draft')
        self.assertTrue(knitter.eval_expr(ast, "rust release notes are out"))
        self.assertFalse(knitter.eval_expr(ast, "rust release notes draft"))
        self.assertFalse(knitter.eval_expr(ast, "go release notes"))

    def test_plan_seeds_picks_selective_branch(self) -> None:
        ast = knitter.parse_expr("(HollowKnight OR Silksong) AND (news OR soon)")
        self.assertEqual(knitter.plan_seeds(ast), ["HollowKnight", "Silksong"])

        phrase_ast = knitter.parse_expr('rust AND "release notes"')
        self.assertEqual(knitter.plan_seeds(phrase_ast), ['"release notes"'])

    def test_store_deduplicates_and_marks_new_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = knitter.Store(pathlib.Path(tmp) / "test.db")
            posts = knitter._parse_rss(SAMPLE_RSS, "https://nitter.example.org", limit=10)

            first = store.save_posts(posts)
            self.assertTrue(all(p.is_new for p in first))

            second = store.save_posts(knitter._parse_rss(SAMPLE_RSS, "https://nitter.example.org", limit=10))
            self.assertTrue(all(not p.is_new for p in second))

            archived = store.all_posts(limit=10)
            self.assertEqual(len(archived), 2)

            store.set_kv("instances", ["https://a.example", "https://b.example"])
            self.assertEqual(store.get_kv("instances", max_age=60), ["https://a.example", "https://b.example"])
            self.assertIsNone(store.get_kv("instances", max_age=0))

    def test_client_respects_robots_and_fails_over_on_bot_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = knitter.Store(pathlib.Path(tmp) / "test.db")
            client = knitter.RSSClient(
                store,
                instances=[
                    "https://disallowed.example",
                    "https://gated.example",
                    "https://good.example",
                ],
            )

            def fake_http(url: str, timeout: float = 8.0, headers=None):
                if url == "https://disallowed.example/robots.txt":
                    return b"User-agent: *\nDisallow: /\n", {}
                if url.endswith("/robots.txt"):
                    return b"User-agent: *\nAllow: /\n", {}
                if url.startswith("https://gated.example/"):
                    return b"<html><title>Verify</title></html>", {}
                if url.startswith("https://good.example/"):
                    return SAMPLE_RSS, {"ETag": '"abc"'}
                raise AssertionError(f"unexpected url: {url}")

            with mock.patch.object(knitter, "_http_get", side_effect=fake_http), mock.patch.object(
                knitter, "REQUEST_DELAY", 0.0
            ):
                posts = client.user("alice", limit=5)

            self.assertEqual(len(posts), 2)
            self.assertEqual(posts[0].source, "https://good.example")
            self.assertIsNotNone(store.get_kv("cd:https://gated.example"))

    def test_client_handles_404_empty_and_429_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = knitter.Store(pathlib.Path(tmp) / "test.db")
            client = knitter.RSSClient(store, instances=["https://one.example", "https://two.example"])

            def fake_http(url: str, timeout: float = 8.0, headers=None):
                if url.endswith("/robots.txt"):
                    return b"", {}
                if url.startswith("https://one.example/"):
                    raise urllib.error.HTTPError(
                        url, 429, "Too Many Requests", {"Retry-After": "120"}, io.BytesIO(b"")
                    )
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))

            with mock.patch.object(knitter, "_http_get", side_effect=fake_http), mock.patch.object(
                knitter, "REQUEST_DELAY", 0.0
            ):
                posts = client.search("rareterm", limit=5)

            self.assertEqual(posts, [])
            self.assertIsNotNone(store.get_kv("cd:https://one.example"))


if __name__ == "__main__":
    unittest.main()
