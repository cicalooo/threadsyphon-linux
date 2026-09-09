from __future__ import annotations

import json
import unittest
from pathlib import Path

from threadsyphon.catalog import CatalogClient
from threadsyphon.models import WatchRule
from threadsyphon.query import filter_threads, match_thread, parse_query, strip_html
from threadsyphon.storage import ConfigStore


class QueryTests(unittest.TestCase):
    def test_parse_and_or_not(self) -> None:
        q = parse_query('title:linux OR body:nvidia -title:meta')
        self.assertEqual(len(q.groups), 2)
        t_ok = {"title": "cool linux tips", "body": "", "no": 1, "images": 0, "replies": 0, "board": "g", "sticky": False, "closed": False}
        t_nv = {"title": "x", "body": "buy nvidia", "no": 2, "images": 0, "replies": 0, "board": "g", "sticky": False, "closed": False}
        t_meta = {"title": "meta linux", "body": "", "no": 3, "images": 0, "replies": 0, "board": "g", "sticky": False, "closed": False}
        self.assertTrue(match_thread(q, t_ok))
        self.assertTrue(match_thread(q, t_nv))
        # second group is body:nvidia only; first group is title:linux AND -title:meta
        # t_meta matches title:linux but negated by -title:meta in same AND group... 
        # wait parse: groups are (title:linux) OR (body:nvidia, -title:meta)? 
        # Actually: title:linux OR body:nvidia -title:meta
        # group1: title:linux
        # group2: body:nvidia, -title:meta
        self.assertTrue(match_thread(q, t_meta))  # matches group1 title:linux

    def test_min_images_and_id(self) -> None:
        q = parse_query("min_images:10 id:42")
        self.assertFalse(match_thread(q, {"title": "", "body": "", "no": 42, "images": 3, "replies": 0, "board": "g", "sticky": False, "closed": False}))
        self.assertTrue(match_thread(q, {"title": "", "body": "", "no": 42, "images": 12, "replies": 0, "board": "g", "sticky": False, "closed": False}))

    def test_quoted_phrase(self) -> None:
        q = parse_query('title:"daily driver"')
        self.assertTrue(match_thread(q, {"title": "My daily driver setup", "body": "", "no": 1, "images": 0, "replies": 0, "board": "g", "sticky": False, "closed": False}))
        self.assertFalse(match_thread(q, {"title": "daily", "body": "driver", "no": 1, "images": 0, "replies": 0, "board": "g", "sticky": False, "closed": False}))

    def test_strip_html(self) -> None:
        self.assertEqual(strip_html("a<br>b <b>c</b>"), "a b c")

    def test_catalog_parse_fixture(self) -> None:
        fixture = [
            {
                "threads": [
                    {"no": 10, "sub": "Hello", "com": "world & stuff", "replies": 2, "images": 3, "time": 1},
                    {"no": 11, "com": "only body", "replies": 0, "images": 1, "time": 2, "sticky": 1},
                ]
            }
        ]
        threads = CatalogClient._parse("g", fixture)
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0].title, "Hello")
        self.assertIn("world & stuff", threads[0].body)
        self.assertTrue(threads[1].sticky)
        q = parse_query("title:hello")
        matched = filter_threads(q, [t.as_match_dict() for t in threads])
        self.assertEqual([m["no"] for m in matched], [10])

    def test_rules_persist(self) -> None:
        path = Path(__file__).parent / "scratch" / "rules_store" / "threads.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        store = ConfigStore(path)
        store.load()
        store.rules = [WatchRule(name="t", board="g", query="linux", interval=90)]
        store.save([], rules=store.rules)
        store2 = ConfigStore(path)
        store2.load()
        self.assertEqual(len(store2.rules), 1)
        self.assertEqual(store2.rules[0].query, "linux")
        self.assertEqual(store2.rules[0].interval, 90)


if __name__ == "__main__":
    unittest.main()


class TitleTagTests(unittest.TestCase):
    def _t(self, title: str, body: str = "") -> dict:
        return {
            "title": title,
            "body": body,
            "no": 1,
            "images": 1,
            "replies": 1,
            "board": "g",
            "sticky": False,
            "closed": False,
        }

    def test_slash_tag_is_title_only(self) -> None:
        q = parse_query("/caig/")
        self.assertTrue(match_thread(q, self._t("/caig/ c ai general")))
        self.assertFalse(match_thread(q, self._t("unrelated", "mentions /caig/ in body")))

    def test_bare_word_still_searches_body(self) -> None:
        q = parse_query("caig")
        self.assertTrue(match_thread(q, self._t("x", "caig in body")))

    def test_title_exact_and_prefix(self) -> None:
        full = self._t("/caig/ c ai general")
        self.assertTrue(match_thread(parse_query('title=:"/caig/ c ai general"'), full))
        self.assertFalse(match_thread(parse_query('title=:"/caig/"'), full))
        self.assertTrue(match_thread(parse_query("title^:/caig/"), full))
        self.assertFalse(match_thread(parse_query("title^:/caig/"), self._t("x /caig/ y")))

    def test_leading_equals_stripped(self) -> None:
        q = parse_query("=/caig/")
        self.assertTrue(match_thread(q, self._t("/caig/ c ai general")))
