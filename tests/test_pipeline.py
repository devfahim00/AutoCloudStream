#!/usr/bin/env python3
"""Offline pipeline tests for AutoCloudStream (no network needed).

A fake site (testsite.local) is served from tests/fixtures/*.html through
FakeFetcher, mimicking a typical WordPress-style movie theme:

    listing.html   /movies/            6 cards (li.thumb) + /page/N/ pagination
    category.html  /category/movies/   4 cards (nav section)
    detail.html    /movies/<slug>/     og meta + .page-body external links
    video.html     watch.examplehost.com/embed/*   iframe + m3u8 in JS

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import generate_provider as gen          # noqa: E402
import site_profiler as prof             # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

LISTING_URL = "https://testsite.local/movies/"


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeFetcher:
    """Maps testsite.local URLs to fixture files."""

    def __init__(self):
        self.hits = []

    def __call__(self, url, referer=None, timeout=20):
        self.hits.append(url)
        p = urlsplit(url)
        netloc, path, query = p.netloc, p.path or "/", (p.query or "")

        if netloc == "testsite.local":
            if "s=" in query:                      # search results
                return _read("listing.html"), {"status": 200, "final_url": url}
            if path.startswith("/movies/page/"):
                return _read("listing.html"), {"status": 200, "final_url": url}
            if re.match(r"^/movies/[^/]+/?$", path):
                return _read("detail.html"), {"status": 200, "final_url": url}
            if path.startswith("/category/movies"):
                return _read("category.html"), {"status": 200, "final_url": url}
            if path in ("/", "/movies", "/movies/"):
                return _read("listing.html"), {"status": 200, "final_url": url}
            return None, {"status": 404, "url": url}
        if netloc == "watch.examplehost.com":
            return _read("video.html"), {"status": 200, "final_url": url}
        return None, {"status": 0, "error": "unreachable", "url": url}


# ---------------------------------------------------------------- profiler

class TestProfiler(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fetcher = FakeFetcher()
        cls.profile = prof.profile_site(
            url=LISTING_URL,
            section_urls=["https://testsite.local/category/movies/"],
            discover=True,
            fetcher=cls.fetcher,
        )

    def test_card_selector(self):
        lis = self.profile["listing"]
        self.assertEqual(lis["card_selector"], "li.thumb")
        self.assertGreaterEqual(lis["card_count"], 6)
        self.assertGreaterEqual(lis["unique_link_count"], 6)

    def test_link_selector(self):
        link = self.profile["listing"]["link"]
        self.assertFalse(link["self"])
        self.assertIn(link["selector"], ("a[href]", "a[href][href]"))

    def test_title_selector(self):
        title = self.profile["listing"]["title"]
        self.assertEqual(title["selector"], "figcaption p.title")
        self.assertEqual(title["method"], "element_text")

    def test_poster(self):
        poster = self.profile["listing"]["poster"]
        self.assertEqual(poster["selector"], "a img")
        self.assertEqual(poster["attr"], "data-src")

    def test_pagination(self):
        pag = self.profile["listing"]["pagination"]
        self.assertEqual(pag["style"], "page_path")
        self.assertEqual(pag["template"], "https://testsite.local/movies/page/{page}/")

    def test_search_form_detected(self):
        search = self.profile["search"]
        self.assertTrue(search["found"])
        self.assertEqual(search["url_template"], "https://testsite.local/?s={query}")
        self.assertTrue(search["verified"])

    def test_detail_url_prefixes(self):
        hints = self.profile["listing"]["detail_url_prefixes"]
        self.assertIn("/movies/", hints)

    def test_sections(self):
        names = [s["name"] for s in self.profile["listing"]["sections"]]
        urls = [s["url"] for s in self.profile["listing"]["sections"]]
        self.assertEqual(names[0], "Home")
        self.assertIn("Movies", names)  # nav link + explicit section
        self.assertIn("https://testsite.local/category/movies/", urls)

    def test_detail_structure(self):
        det = self.profile["detail"]
        self.assertTrue(det["samples"])
        self.assertTrue(det["og_title"])
        self.assertTrue(det["og_image"])
        self.assertEqual(det["title_selector"], "h1.page-title")
        self.assertEqual(det["desc_selector"], ".description")
        self.assertEqual(det["detail_links_selector"], ".page-body a[href]")
        self.assertIn("watch.examplehost.com", det["stream_hosts"])

    def test_video_scan(self):
        vid = self.profile["video"]
        self.assertTrue(vid["scanned"])
        self.assertEqual(vid["mode"], "direct_m3u8")
        self.assertTrue(any("master.m3u8" in u for u in vid["found_m3u8"]))
        self.assertEqual(vid["sample_url"],
                         "https://watch.examplehost.com/embed/iron-man-1080p")

    def test_site_name(self):
        self.assertEqual(self.profile["site"]["name"], "Testsite")
        self.assertEqual(self.profile["site"]["main_url"], "https://testsite.local")


# ---------------------------------------------------------------- generator

class TestGenerator(unittest.TestCase):
    PROFILE = None

    @classmethod
    def setUpClass(cls):
        cls.fetcher = FakeFetcher()
        cls.PROFILE = prof.profile_site(
            url=LISTING_URL,
            section_urls=["https://testsite.local/category/movies/"],
            fetcher=cls.fetcher,
        )

    def _opts(self, **kw):
        opts = gen.argparse.Namespace(
            profile="profile.json",
            out=kw.pop("out", tempfile.mkdtemp()),
            package=kw.pop("package", None),
            class_name=kw.pop("class_name", None),
            name=kw.pop("name", None),
            author="devfahim00",
            lang=None,
            description="Auto-generated by AutoCloudStream",
            status=3,
            icon=None,
            series=kw.pop("series", False),
            telegram_url=kw.pop("telegram_url", gen.DEFAULT_TELEGRAM_URL),
            telegram_poster=gen.DEFAULT_TELEGRAM_POSTER,
            single=kw.pop("single", False),
            profile_path="profile.json",
        )
        return opts

    def test_render_engine(self):
        tpl = "A={{a}}{{#on}} yes{{/on}}{{^on}} no{{/on}}{{#off}} hidden{{/off}}"
        out = gen.render(tpl, {"a": 1, "on": True, "off": False})
        self.assertEqual(out, "A=1 yes")

    def test_module_files_created(self):
        files = gen.generate(self.PROFILE, self._opts())
        names = sorted(files)
        self.assertIn("TestsiteProvider.kt", names)
        self.assertIn("TestsitePlugin.kt", names)
        self.assertIn("build.gradle.kts", names)
        self.assertIn("src/main/AndroidManifest.xml", names)

    def test_provider_kt_content(self):
        files = gen.generate(self.PROFILE, self._opts())
        kt = files["TestsiteProvider.kt"]

        # no unresolved placeholders, balanced braces
        self.assertFalse(re.search(r"\{\{[^}]*\}\}", kt), "leftover placeholders")
        self.assertEqual(kt.count("{"), kt.count("}"), "unbalanced braces")
        self.assertNotIn("None", kt)

        # identity + constants
        self.assertIn("package com.testsite", kt)
        self.assertIn("class TestsiteProvider : MainAPI()", kt)
        self.assertIn('override var mainUrl = "https://testsite.local"', kt)
        self.assertIn('CARD_SELECTOR = "li.thumb"', kt)
        self.assertIn('POSTER_ATTRS = listOf("data-src", "src")', kt)
        self.assertIn('DETAIL_TITLE_SELECTOR: String? = "h1.page-title"', kt)
        self.assertIn('DETAIL_LINKS_SELECTOR = ".page-body a[href]"', kt)
        self.assertIn('"watch.examplehost.com"', kt)
        self.assertIn('DETAIL_HREF_HINTS = listOf("/movies/")', kt)

        # main page sections + pagination style
        self.assertIn('mainPageOf(', kt)
        self.assertIn('"$mainUrl/movies/" to "Home"', kt)
        self.assertIn('"$mainUrl/category/movies/" to "Movies"', kt)
        self.assertIn('page/$page/', kt)          # page_path pagination body

        # search uses Uri.encode
        self.assertIn('Uri.encode(query)', kt)
        self.assertIn('"$mainUrl/?s=" + Uri.encode(query)', kt)

        # telegram block present by default
        self.assertIn("telegramCard()", kt)
        self.assertIn('FIRST_SECTION_NAME = "Home"', kt)

        # generic extractor present
        self.assertIn("extractViaWebView", kt)
        self.assertIn("unpackAllPackedScripts", kt)
        self.assertIn("loadExtractor(", kt)
        self.assertIn("MANUAL REVIEW", kt)
        self.assertIn("master.m3u8", kt)  # profiler finding embedded as comment

    def test_single_mode_no_telegram(self):
        opts = self._opts(single=True, telegram_url="")
        files = gen.generate(self.PROFILE, opts)
        kt = files["TestsiteProvider.kt"]
        self.assertNotIn("telegramCard", kt)
        self.assertNotIn("import android.content.Context", kt)
        self.assertIn("return emptyList()", kt)  # search still present
        # write and check only Provider.kt is emitted
        written = gen.write_outputs(files, opts, "com.testsite")
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].endswith("TestsiteProvider.kt"))

    def test_series_flag_adds_tvseries(self):
        files = gen.generate(self.PROFILE, self._opts(series=True))
        kt = files["TestsiteProvider.kt"]
        self.assertIn("TvType.TvSeries", kt.split("supportedTypes")[1].split(")")[0])

    def test_plugin_and_gradle(self):
        files = gen.generate(self.PROFILE, self._opts())
        plugin = files["TestsitePlugin.kt"]
        self.assertIn("class TestsitePlugin : Plugin()", plugin)
        self.assertIn("registerMainAPI(TestsiteProvider())", plugin)

        gradle = files["build.gradle.kts"]
        self.assertIn('authors     = listOf("devfahim00")', gradle)
        self.assertIn("status  = 3", gradle)
        self.assertIn('tvTypes = listOf("Movie")', gradle)
        self.assertIn('iconUrl = "https://testsite.local/favicon.ico"', gradle)

    def test_write_module_layout(self):
        opts = self._opts()
        files = gen.generate(self.PROFILE, opts)
        written = gen.write_outputs(files, opts, "com.testsite")
        expected = {
            os.path.join("Testsite", "build.gradle.kts"),
            os.path.join("Testsite", "src", "main", "AndroidManifest.xml"),
            os.path.join("Testsite", "src", "main", "kotlin", "com", "testsite",
                         "TestsitePlugin.kt"),
            os.path.join("Testsite", "src", "main", "kotlin", "com", "testsite",
                         "TestsiteProvider.kt"),
        }
        rel = {os.path.relpath(p, opts.out) for p in written}
        self.assertTrue(expected.issubset(rel), rel)

    def test_invalid_package_rejected(self):
        with self.assertRaises(gen.GeneratorError):
            gen.build_context(self.PROFILE, self._opts(package="Bad Package"))


# ---------------------------------------------------------------- end-to-end

class TestEndToEnd(unittest.TestCase):

    def test_profile_json_roundtrip(self):
        fetcher = FakeFetcher()
        profile = prof.profile_site(url=LISTING_URL, fetcher=fetcher)
        blob = json.dumps(profile)
        loaded = json.loads(blob)
        self.assertEqual(loaded["listing"]["card_selector"], "li.thumb")

        opts = gen.argparse.Namespace(
            profile="x", out=tempfile.mkdtemp(), package=None, class_name=None,
            name=None, author="devfahim00", lang=None,
            description="Auto-generated by AutoCloudStream", status=3, icon=None,
            series=False, telegram_url="", telegram_poster="",
            single=True, profile_path="x",
        )
        files = gen.generate(loaded, opts)
        self.assertIn("TestsiteProvider.kt", files)


if __name__ == "__main__":
    unittest.main(verbosity=2)
