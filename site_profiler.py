#!/usr/bin/env python3
"""
AutoCloudStream — Stage 1: Site Profiler
========================================

Scans a video/movie streaming site and produces a JSON "site profile"
describing how its listing pages, detail pages and video pages are
structured. The profile is consumed by generate_provider.py (Stage 3)
to emit a CloudStream MainAPI provider (.kt).

What it tries to detect
-----------------------
1. Listing page:
   - the repeated "card" element (tag+class combo appearing >= N times)
   - link / title / poster selectors inside the card
   - pagination style (/page/N/  |  /N/  |  ?page=N  |  none)
   - the site's search endpoint (form-based first, then common patterns)
   - extra home-page sections (nav/category links that also contain cards)
2. Detail page:
   - og:title / og:image / og:description meta tags
   - on-page title / description selectors
   - outgoing "stream host" links (external embed/download hosts)
   - the container selector that holds playable / episode links
3. Video page (optional, auto-discovered from detail page):
   - direct .m3u8 / .mp4 URLs
   - iframe embeds
   - p.a.c.k.e.r obfuscated player signatures

Usage
-----
    python site_profiler.py --url https://example.com/movies -o profile.json
    python site_profiler.py --url https://example.com/ --discover-sections 6
    python site_profiler.py --url ... --video-url https://host.com/embed/xyz

Video-link extraction itself stays manual (per-site tokens, obfuscated
players are different on every site) — but everything above is detected
automatically so you only have to write that one part yourself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper  # optional: pip install cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

SCHEMA_VERSION = 1
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# hrefs that are never detail pages
SKIP_HREF_RE = re.compile(
    r"javascript:|^#|^mailto:|^tel:|/category/|/tag/|/tags/|/page/\d|/author/"
    r"|/feed|\.xml|\.rss|disclaimer|privacy|terms|about|contact"
    r"|how-to-download|request-a-movie|join-our-group|dmca"
    r"|whatsapp\.com|telegram|t\.me|facebook\.com|twitter\.com|x\.com"
    r"|instagram\.com|reddit\.com|discord\.gg|pinterest\.com",
    re.I,
)

# hosts that must never be treated as stream hosts
JUNK_HOSTS = (
    "google", "gstatic", "doubleclick", "googletagmanager", "google-analytics",
    "googlesyndication", "adservice", "amazon-adsystem", "cloudflareinsights",
    "facebook", "twitter", "x.com", "instagram", "reddit", "discord",
    "whatsapp", "t.me", "telegram", "pinterest", "youtube.com", "youtu.be",
    "recaptcha", "fontawesome", "fonts.gstatic", "cdnjs", "unpkg", "jsdelivr",
    "w3.org", "schema.org", "googleapis.com",
)

# class-name hints that a repeated element is a content card
CARD_CLASS_HINTS = (
    "item", "card", "thumb", "post", "movie", "video", "episode", "block",
    "box", "film", "flw", "season", "ep-", "listing", "result",
)

TITLE_CLASS_RE = re.compile(r"tit|name|caption|label", re.I)

# words that are not real titles
GENERIC_TEXT = {
    "read more", "view more", "more", "download", "watch", "watch now",
    "play", "play now", "details", "see more", "latest", "home", "search",
    "loading", "continue reading",
}

NAV_ANCESTOR_TAGS = {"nav", "header", "footer"}

PLAYER_HINTS = (
    "jwplayer", "videojs", "video-js", "plyr", "hls.js", "fluidplayer",
    "fluid-player", "video-player", "player-instance", "dplayer",
)

M3U8_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s"'<>\\]*)?""", re.I
)
PACKER_RE = re.compile(r"eval\(function\(p,a,c,k,e,[dr]\)", re.I)
IFRAME_SRC_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)


class ProfilerError(Exception):
    """Raised when a site cannot be profiled at all."""


# ----------------------------------------------------------------------------
# fetching
# ----------------------------------------------------------------------------

def parse_html(html: str) -> BeautifulSoup:
    """Parse with lxml when available, else the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def default_fetcher(url: str, referer: str | None = None, timeout: int = 20):
    """Fetch a URL with browser-like headers.

    Returns (html | None, info dict). Never raises — failures are reported
    through the info dict so one bad section/link never kills the profile.
    """
    headers = dict(BASE_HEADERS)
    if referer:
        headers["Referer"] = referer
    info = {"url": url, "status": 0, "cloudflare": False}

    def _is_cf(resp) -> bool:
        if resp is None:
            return False
        body = resp.text[:4000].lower()
        return (
            resp.status_code in (403, 503)
            and ("cloudflare" in body or "cf-ray" in (resp.headers or {}))
        )

    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if _is_cf(resp) and HAS_CLOUDSCRAPER:
            try:
                scraper = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "desktop": True}
                )
                resp = scraper.get(url, headers=headers, timeout=timeout)
            except Exception:
                pass
        info["status"] = resp.status_code
        info["final_url"] = resp.url
        info["cloudflare"] = _is_cf(resp)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and ("html" in ctype or "xml" in ctype or not ctype):
            return resp.text, info
        if resp.status_code == 200:
            # JSON API responses are still useful for regex scanning
            info["json"] = True
            return resp.text, info
        return None, info
    except Exception as exc:
        info["error"] = str(exc)
        return None, info


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def host_of(url: str) -> str:
    return (urlsplit(url).netloc or "").lower().split("@")[-1].split(":")[0]


def is_internal(href: str, base_host: str) -> bool:
    """True for relative / protocol-relative URLs and same-host absolutes."""
    if not href:
        return False
    if href.startswith("//"):
        return True  # will be resolved later; assume internal-ish
    if re.match(r"^https?://", href, re.I):
        h = host_of(href)
        return h == base_host or h.endswith("." + base_host)
    return not href.startswith(("javascript:", "mailto:", "tel:", "data:", "#"))


def normalize_url(url: str) -> str:
    """Drop fragment + trailing slash for dedupe comparisons."""
    p = urlsplit(url.strip())
    path = p.path or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


# ----------------------------------------------------------------------------
# Stage 1a — card detection on the listing page
# ----------------------------------------------------------------------------

def _sig_of(el) -> str | None:
    """tag.class1.class2 signature (only for classed elements)."""
    classes = el.get("class") or []
    if not classes:
        return None
    return el.name + "." + ".".join(sorted(classes)[:3])


def _candidate_groups(soup: BeautifulSoup, min_cards: int):
    """Build (selector, [elements]) candidate groups.

    Strategy A: direct tag+class signatures  (li.thumb, div.item ...)
    Strategy B: unclassed children of a classed "list" container
               (.video-list li, .card-grid article ...)
    """
    groups: dict[str, list] = {}

    # --- strategy A ---
    for el in soup.find_all(True):
        if el.name in ("script", "style", "link", "meta", "br", "hr", "path",
                       "svg", "input", "option", "noscript"):
            continue
        sig = _sig_of(el)
        if sig:
            groups.setdefault(sig, []).append(el)

    # --- strategy B: unclassed tags repeated under one classed parent ---
    for parent in soup.find_all(True):
        pclasses = parent.get("class") or []
        if not pclasses:
            continue
        by_tag: Counter = Counter()
        for child in parent.children:
            if getattr(child, "name", None) and not (child.get("class") or []):
                by_tag[child.name] += 1
        for tag, count in by_tag.items():
            if count >= min_cards and tag not in ("script", "style", "br", "tr", "li", "option"):
                # li/span without class usually means strategy A found the real
                # card; allow article/div/a/p which some themes leave unclassed
                sel = f".{pclasses[0]} {tag}"
                groups.setdefault(sel, []).extend(
                    c for c in parent.children
                    if getattr(c, "name", None) == tag and not (c.get("class") or [])
                )

    # keep only groups that reach the threshold
    groups = {sel: els for sel, els in groups.items() if len(els) >= min_cards}

    # resolve nesting: if most members live inside another member of the same
    # group (theme reuses the class on wrapper + inner), keep only outermost
    resolved = {}
    for sel, els in groups.items():
        ids = {id(e) for e in els}
        outer = [e for e in els if id(e.parent) not in ids]
        resolved[sel] = outer if outer and len(outer) >= min_cards else els
    return {sel: els for sel, els in resolved.items() if len(els) >= min_cards}


def _skip_href(href: str) -> bool:
    return bool(href) and bool(SKIP_HREF_RE.search(href))


def _primary_link(card, base_host: str):
    """Best detail-page link inside a card: prefer one wrapping the poster."""
    links = card.find_all("a", href=True)
    internal = [a for a in links if is_internal(a["href"].strip(), base_host)
                and not _skip_href(a["href"].strip())]
    if not internal:
        return None
    for a in internal:                      # 1. link that wraps an image
        if a.find("img"):
            return a
    for a in internal:                      # 2. link inside a heading
        if a.find_parent(["h1", "h2", "h3", "h4", "h5"]):
            return a
    for a in internal:                      # 3. link with real text
        if a.get_text(" ", strip=True):
            return a
    return internal[0]


def _in_nav(el) -> bool:
    p = el.parent
    while p is not None and getattr(p, "name", None):
        if p.name in NAV_ANCESTOR_TAGS:
            return True
        pclasses = p.get("class") or []
        if any(re.search(r"(nav|menu|header|footer|breadcrumb)", c, re.I) for c in pclasses):
            return True
        p = p.parent
    return False


def _score_group(selector: str, elements: list, base_host: str):
    """Heuristic score for 'is this repeated element a content card?'"""
    n = len(elements)
    link_ok = img_ok = title_ok = nav_pen = 0
    hrefs, desc_counts, text_lens = [], [], []

    for el in elements[:40]:
        link = el if el.name == "a" and el.get("href") else _primary_link(el, base_host)
        if link is not None:
            link_ok += 1
            hrefs.append(link["href"].strip())
        if el.find("img"):
            img_ok += 1
        desc_counts.append(len(el.find_all(True)))
        text = el.get_text(" ", strip=True)
        text_lens.append(len(text))
        if _in_nav(el):
            nav_pen += 1

    # titles can come from link text, headings or img alt
    title_ok = sum(
        1 for el in elements[:20]
        if (el.find("img") and (el.find("img").get("alt") or "").strip())
        or any((h.get_text(" ", strip=True)) for h in el.find_all(["h1", "h2", "h3", "h4", "h5"]))
        or (_primary_link(el, base_host) is not None
            and _primary_link(el, base_host).get_text(" ", strip=True))
    )

    k = max(n, 1)
    link_ratio = link_ok / k
    img_ratio = img_ok / k
    unique_ratio = len({normalize_url(urljoin("https://" + base_host, h)) if not re.match(r"^https?://", h) else normalize_url(h) for h in hrefs}) / max(len(hrefs), 1)
    avg_desc = sum(desc_counts) / k
    avg_text = sum(text_lens) / k

    score = 0.0
    score += 30.0 * link_ratio
    score += 18.0 * img_ratio
    score += 14.0 * min(title_ok / min(n, 20), 1.0)
    score += 12.0 * (unique_ratio if hrefs else 0.0)
    score -= 25.0 * (nav_pen / k)
    if avg_desc > 45:
        score -= 6.0
    if avg_text > 350:
        score -= 8.0
    if any(h in selector.lower() for h in CARD_CLASS_HINTS):
        score += 6.0

    stats = {
        "count": n,
        "link_ratio": round(link_ratio, 2),
        "img_ratio": round(img_ratio, 2),
        "unique_href_ratio": round(unique_ratio, 2),
        "nav_ratio": round(nav_pen / k, 2),
    }
    return score, stats


def detect_cards(soup: BeautifulSoup, base_host: str, min_cards: int = 5):
    """Return (selector, elements, stats, score) of the best card candidate."""
    groups = _candidate_groups(soup, min_cards)
    if not groups:
        return None, [], {}, 0.0

    best_sel, best_els, best_stats, best_score = None, [], {}, -1.0
    for sel, els in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        score, stats = _score_group(sel, els, base_host)
        if score > best_score:
            best_sel, best_els, best_stats, best_score = sel, els, stats, score
    return best_sel, best_els, best_stats, best_score


# ----------------------------------------------------------------------------
# Stage 1b — sub-selectors inside the card
# ----------------------------------------------------------------------------

def _rel_path(card, el) -> str:
    """Minimal CSS path from card down to el (e.g. 'figcaption a')."""
    parts = []
    cur = el
    while cur is not None and cur is not card and cur.name not in ("[document]", "html", "body"):
        classes = cur.get("class") or []
        seg = cur.name + ("." + classes[0] if classes else "")
        parts.append(seg)
        cur = cur.parent
    return " ".join(reversed(parts))


def _most_common(paths: list) -> tuple:
    return Counter(paths).most_common(1)[0] if paths else (None, 0)


def detect_link_selector(cards, base_host: str):
    """Detect (selector, self_link) for the detail link inside a card."""
    paths, self_link = [], 0
    sample_hrefs = []
    for card in cards[:15]:
        if card.name == "a" and card.get("href"):
            self_link += 1
            sample_hrefs.append(card["href"].strip())
            continue
        link = _primary_link(card, base_host)
        if link is None:
            continue
        sample_hrefs.append(link["href"].strip())
        p = _rel_path(card, link)
        if p:
            # last segment is the <a> itself — add [href] to it
            paths.append(p + "[href]")
    if self_link >= len(cards[:15]) * 0.6:
        return None, True, sample_hrefs        # the card itself is the <a>
    if not paths:
        return "a[href]", False, sample_hrefs  # safe default
    path, cnt = _most_common(paths)
    total = max(len(paths), 1)
    if cnt / total < 0.6:
        return "a[href]", False, sample_hrefs  # inconsistent structure → generic
    return path, False, sample_hrefs


def detect_title_selector(cards, link_selector, self_link, base_host: str):
    """Detect (selector|None, method) for the card title."""
    heading_paths, link_text_hits, alt_hits = [], 0, 0

    for card in cards[:15]:
        # 1. heading or .title-ish element with text
        holder = None
        for el in card.find_all(True):
            if el.name in ("h1", "h2", "h3", "h4", "h5") or (
                el.get("class") and TITLE_CLASS_RE.search(" ".join(el["class"]))
            ):
                txt = el.get_text(" ", strip=True)
                if txt and len(txt) < 160 and txt.lower() not in GENERIC_TEXT:
                    holder = el
                    break
        if holder is not None:
            p = _rel_path(card, holder)
            if p:
                heading_paths.append(p)
        # 2. link text
        link = None
        if self_link and card.name == "a":
            link = card
        elif link_selector:
            try:
                link = card.select_one(link_selector)
            except Exception:
                link = None
        if link is None:
            link = _primary_link(card, base_host)
        if link is not None:
            t = link.get_text(" ", strip=True)
            if t and t.lower() not in GENERIC_TEXT and len(t) < 160:
                link_text_hits += 1
        # 3. img alt
        img = card.find("img")
        if img is not None:
            alt = (img.get("alt") or img.get("title") or "").strip()
            if alt and len(alt) < 160:
                alt_hits += 1

    total = max(len(cards[:15]), 1)
    if heading_paths and len(heading_paths) / total >= 0.6:
        path, cnt = _most_common(heading_paths)
        if cnt / max(len(heading_paths), 1) >= 0.6:
            return path, "element_text"
    if link_text_hits / total >= 0.6:
        return None, "link_text"
    if alt_hits / total >= 0.6:
        return None, "img_alt"
    return None, "link_text"


POSTER_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-poster", "src")


def detect_poster(cards):
    """Detect (selector, attr, count) for the poster image inside a card."""
    paths, attr_votes = [], Counter()

    def _valid(val: str) -> bool:
        if not val:
            return False
        v = val.strip().lower()
        if v.startswith("data:image"):
            return False
        if any(x in v for x in ("placeholder", "loading.gif", "blank", "lazy_", "default")):
            return False
        return True

    img_count = 0
    for card in cards[:15]:
        img = card.find("img")
        if img is None:
            continue
        img_count += 1
        p = _rel_path(card, img)
        if p:
            paths.append(p)
        for attr in POSTER_ATTRS:
            if _valid(img.get(attr, "")):
                attr_votes[attr] += 1
    if img_count == 0:
        return None, None, 0

    # prefer the attr that is valid most often; src wins ties (simplest)
    best_attr, best_votes = "src", 0
    for attr in POSTER_ATTRS:
        v = attr_votes.get(attr, 0)
        if v > best_votes:
            best_attr, best_votes = attr, v
    if best_votes == 0:
        return "img", "src", img_count

    path, cnt = _most_common(paths)
    if path and cnt / max(len(paths), 1) >= 0.7 and path.split()[-1].startswith("img"):
        selector = path
    else:
        selector = "img"
    return selector, best_attr, img_count


# ----------------------------------------------------------------------------
# Stage 1c — pagination
# ----------------------------------------------------------------------------

NEXT_TEXTS = {"next", "»", "›", ">", "next »", "older", "load more", "more"}


def detect_pagination(soup: BeautifulSoup, listing_url: str):
    """Detect pagination style + build a URL template with {page}."""
    base = listing_url
    next_href = None

    for sel in ("a[rel=next]", "a.next", "a.page-next", ".pagination .next a",
                ".nav-links .next", "li.next a", ".next.page-numbers",
                ".pagination a[rel=next]"):
        a = soup.select_one(sel)
        if a is not None and a.get("href"):
            next_href = a["href"]
            break
    if next_href is None:
        for a in soup.select(".pagination a, .page-numbers, .nav-links a, ul.page-numbers li a"):
            txt = a.get_text(" ", strip=True).lower()
            if txt in NEXT_TEXTS and a.get("href"):
                next_href = a["href"]
                break
    if next_href is None:
        # link whose text is literally "2" inside a pagination container
        for a in soup.select(".pagination a, .page-numbers a, .nav-links a"):
            if a.get_text(strip=True) == "2" and a.get("href"):
                next_href = a["href"]
                break

    if not next_href:
        return {"style": "none", "template": None, "next_selector": None}

    url = urljoin(listing_url, next_href.strip())
    url = url.split("#")[0]
    base_noslash = base.rstrip("/")

    # /page/2/ style (WordPress-ish)
    m = re.search(r"/page/(\d+)/?", url)
    if m:
        template = re.sub(r"/page/\d+/?", "/page/{page}/", url)
        return {"style": "page_path", "template": template,
                "next_selector": "a[rel=next], a.next, .next.page-numbers"}

    # ?page=2 / ?p=2 style
    q = dict(parse_qsl(urlsplit(url).query))
    for param in ("page", "paged", "p", "pg", "start", "offset"):
        if param in q:
            sep = "&" if "?" in base else "?"
            return {"style": "query", "template": f"{base}{sep}{param}={{page}}",
                    "param": param,
                    "next_selector": "a[rel=next], a.next"}

    # trailing /2/ style (tube sites: /videos/2/)
    m = re.search(r"(\d+)/?$", url.rstrip("?"))
    if m and m.group(1) == "2":
        stripped = re.sub(r"(\d+)/?$", "", url.rstrip("?")).rstrip("/")
        if normalize_url(stripped) == normalize_url(base_noslash):
            template = re.sub(r"(\d+)/?$", "{page}/", url.rstrip("?"))
            return {"style": "number_suffix", "template": template,
                    "next_selector": "a[rel=next], a.next, .pagination a"}
    return {"style": "none", "template": None, "next_selector": None}


# ----------------------------------------------------------------------------
# Stage 1d — search endpoint
# ----------------------------------------------------------------------------

SEARCH_INPUT_NAMES = ("s", "search", "q", "query", "keyword", "kw", "term",
                      "search_term", "search_query", "searchword")


def _check_search_page(html: str, card_selector: str | None) -> bool:
    if not html:
        return False
    soup = parse_html(html)
    if card_selector:
        try:
            if soup.select(card_selector):
                return True
        except Exception:
            pass
    # fall back: any repeated classed element with links (search page layout
    # sometimes differs); good enough signal that the endpoint is live
    return bool(soup.select("a[href]")) and len(soup.get_text(" ", strip=True)) > 200


def detect_search(soup: BeautifulSoup, listing_url: str, card_selector: str | None,
                  sample_query: str, fetcher, timeout: int = 20):
    """Find the search endpoint: <form> first, then common URL patterns."""
    base = origin_of(listing_url)

    def _verify(template: str):
        try:
            probe = template.replace("{query}", sample_query)
        except Exception:
            return False
        html, info = fetcher(probe, referer=listing_url, timeout=timeout)
        return _check_search_page(html, card_selector)

    # 1) real search form on the page
    for form in soup.find_all("form"):
        method = (form.get("method") or "get").lower()
        if method != "get":
            continue
        inp = None
        for cand in form.find_all("input"):
            name = (cand.get("name") or "").lower()
            if name in SEARCH_INPUT_NAMES or cand.get("type") == "search":
                inp = cand
                break
        if inp is None or not inp.get("name"):
            continue
        action = urljoin(listing_url, form.get("action") or listing_url)
        sep = "&" if "?" in action else "?"
        template = f"{action}{sep}{inp['name']}={{query}}"
        verified = _verify(template)
        return {"found": True, "url_template": template, "method": "form",
                "verified": verified}

    # 2) common search URL patterns
    probes = [
        f"{base}/?s={{query}}",
        f"{base}/search/{{query}}",
        f"{base}/search/{{query}}/",
        f"{base}/search?q={{query}}",
        f"{base}/?search={{query}}",
        f"{base}/?q={{query}}",
        f"{base}/search.php?q={{query}}",
    ]
    for template in probes:
        if _verify(template):
            return {"found": True, "url_template": template, "method": "probe",
                    "verified": True}
    return {"found": False, "url_template": None, "method": None, "verified": False}


# ----------------------------------------------------------------------------
# Stage 1e — detail page analysis
# ----------------------------------------------------------------------------

def detect_detail_links_container(soup: BeautifulSoup, main_host: str):
    """Find the selector of the container holding stream/episode links."""
    keepable = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        if _skip_href(href):
            continue
        host = host_of(href)
        if host == main_host:
            continue
        if any(j in host for j in JUNK_HOSTS):
            continue
        keepable.append(a)
    if len(keepable) < 2:
        return None, Counter()

    host_counter = Counter(host_of(a["href"]) for a in keepable)

    # best ancestor container with a class
    container_votes: Counter = Counter()
    for a in keepable:
        p = a.parent
        depth = 0
        while p is not None and depth < 3 and getattr(p, "name", None):
            classes = p.get("class") or []
            if classes:
                container_votes[f".{classes[0]} a[href]"] += 1
                break
            p = p.parent
            depth += 1
    best, votes = container_votes.most_common(1)[0] if container_votes else (None, 0)
    selector = best if votes >= 3 else None
    return selector, host_counter


def profile_details(detail_urls: list, listing_url: str, fetcher, timeout: int = 20,
                    max_pages: int = 2):
    """Fetch up to N detail pages and extract structure info."""
    out = {
        "samples": [],
        "title_selector": None,
        "desc_selector": None,
        "og_title": False,
        "og_image": False,
        "og_description": False,
        "meta_description": False,
        "stream_hosts": [],
        "detail_links_selector": None,
        "episode_links_detected": False,
        "iframes": [],
    }
    host_counter: Counter = Counter()
    container_votes: Counter = Counter()
    title_paths, desc_paths = [], []

    for url in detail_urls[:max_pages]:
        html, info = fetcher(url, referer=listing_url, timeout=timeout)
        if not html:
            continue
        soup = parse_html(html)
        out["samples"].append(url)
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            out["og_title"] = True
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            out["og_image"] = True
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            out["og_description"] = True
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            out["meta_description"] = True

        h1 = soup.find("h1") or soup.find("h2")
        if h1 is not None and h1.get_text(strip=True):
            classes = h1.get("class") or []
            seg = h1.name + ("." + classes[0] if classes else "")
            # doc-level selector
            title_paths.append(seg if classes else h1.name)

        # description-ish elements
        for el in soup.select("[class*=description], [class*=synopsis], [class*=plot], [class*=story]"):
            if el.get_text(" ", strip=True) and len(el.get_text(" ", strip=True)) > 40:
                classes = el.get("class") or []
                if classes:
                    desc_paths.append(f".{classes[0]}")
                    break

        # stream hosts + link container
        sel, hosts = detect_detail_links_container(soup, host_of(listing_url))
        host_counter.update(hosts)
        if sel:
            container_votes[sel] += 1

        for ifr in soup.find_all("iframe", src=True):
            src = ifr["src"].strip()
            if src.startswith("http") and not any(j in host_of(src) for j in JUNK_HOSTS):
                out["iframes"].append(src)

        # episode-ish internal links
        ep = soup.select("a[href*='episode'], a[href*='season'], a[href*='/ep']")
        if len(ep) >= 3:
            out["episode_links_detected"] = True

    if title_paths:
        out["title_selector"], cnt = _most_common(title_paths)
    if desc_paths:
        out["desc_selector"], cnt = _most_common(desc_paths)
    if container_votes:
        out["detail_links_selector"], cnt = _most_common(list(container_votes))
    out["stream_hosts"] = [h for h, _ in host_counter.most_common(5)
                           if not any(j in h for j in JUNK_HOSTS)]
    out["iframes"] = out["iframes"][:5]
    return out


# ----------------------------------------------------------------------------
# Stage 1f — video page scan (regex only, no JS execution)
# ----------------------------------------------------------------------------

def profile_video_page(url: str, referer: str | None, fetcher, timeout: int = 20):
    """Scan a player/embed page for direct streams, iframes, packers."""
    out = {
        "scanned": False,
        "sample_url": url,
        "mode": "unknown",
        "found_m3u8": [],
        "found_mp4": [],
        "found_iframes": [],
        "packed_script": False,
        "player_hints": [],
        "notes": "",
    }
    if not url:
        return out
    html, info = fetcher(url, referer=referer, timeout=timeout)
    out["scanned"] = True
    if not html:
        out["notes"] = f"page unreachable (status={info.get('status', 0)})"
        return out

    m3u8 = []
    mp4 = []
    for u in M3U8_RE.findall(html):
        u = u.replace("\\/", "/").replace("&amp;", "&")
        (m3u8 if ".m3u8" in u.lower() else mp4).append(u)
    out["found_m3u8"] = list(dict.fromkeys(m3u8))[:5]
    out["found_mp4"] = list(dict.fromkeys(mp4))[:5]

    soup = parse_html(html)
    iframes = [i["src"].strip() for i in soup.find_all("iframe", src=True)]
    if not iframes:
        iframes = IFRAME_SRC_RE.findall(html)
    out["found_iframes"] = [
        s for s in dict.fromkeys(iframes)
        if s.startswith("http") and not any(j in host_of(s) for j in JUNK_HOSTS)
    ][:5]

    out["packed_script"] = bool(PACKER_RE.search(html))
    out["player_hints"] = [h for h in PLAYER_HINTS if h in html.lower()]

    if out["found_m3u8"] or out["found_mp4"]:
        out["mode"] = "direct_m3u8" if out["found_m3u8"] else "direct_mp4"
        out["notes"] = "Direct stream URL found in page source — loadLinks should work out of the box."
    elif out["packed_script"]:
        out["mode"] = "packed"
        out["notes"] = "Packed (p.a.c.k.e.r) player detected — generic unpacker in the template usually handles this."
    elif out["found_iframes"]:
        out["mode"] = "iframe"
        out["notes"] = "Third-party iframe embeds detected — CloudStream loadExtractor() may support these hosts."
    else:
        out["notes"] = "No direct stream in raw HTML — player is likely JS-rendered. Try WebViewResolver (already in template) or write manual extraction."
    return out


# ----------------------------------------------------------------------------
# Stage 1g — home-page section discovery
# ----------------------------------------------------------------------------

def discover_sections(soup: BeautifulSoup, listing_url: str, card_selector: str,
                      fetcher, max_sections: int = 6, min_cards: int = 3,
                      timeout: int = 20):
    """Try same-site nav/category links and keep those that contain cards."""
    base_host = host_of(listing_url)
    candidates, seen = [], {normalize_url(listing_url)}

    def _add(a):
        href = (a.get("href") or "").strip()
        if not href or _skip_href(href):
            return
        if not is_internal(href, base_host):
            return
        full = urljoin(listing_url, href).split("#")[0]
        if normalize_url(full) in seen:
            return
        name = a.get_text(" ", strip=True)
        if not name or len(name) > 40:
            return
        seen.add(normalize_url(full))
        candidates.append((full, name))

    # nav / menu links first (site's own section order), then all internal links
    for a in soup.select("nav a[href], [class*=menu] a[href], header a[href]"):
        _add(a)
    if len(candidates) < 10:
        for a in soup.find_all("a", href=True):
            _add(a)

    sections = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetcher, url, None, timeout): (url, name)
            for url, name in candidates[:18]
        }
        for fut in as_completed(futures):
            url, name = futures[fut]
            try:
                html, info = fut.result()
            except Exception:
                continue
            if not html:
                continue
            try:
                s = parse_html(html)
                if len(s.select(card_selector)) >= min_cards:
                    sections.append({"name": name.title()[:32], "url": url})
            except Exception:
                continue
    # keep the site's nav order (candidates order), cap sections
    ordered = [s for c in candidates for s in sections if s["url"] == c[0]]
    return ordered[:max_sections]


# ----------------------------------------------------------------------------
# confidence + report
# ----------------------------------------------------------------------------

def _grade(conf: float) -> str:
    return "high" if conf >= 0.75 else ("medium" if conf >= 0.45 else "low")


def build_confidence(card_score, card_stats, poster, search, detail, pagination):
    card_conf = min(card_score / 85.0, 1.0) if card_score else 0.0
    link_conf = card_stats.get("link_ratio", 0) if card_stats else 0
    unique_conf = card_stats.get("unique_href_ratio", 0) if card_stats else 0
    poster_conf = 1.0 if poster and poster.get("attr") else 0.0
    if poster and poster.get("count"):
        poster_conf = min(poster["count"] / 10.0, 1.0) * (1.0 if poster.get("attr") else 0.3)
    search_conf = 0.9 if search.get("verified") else (0.55 if search.get("found") else 0.0)
    detail_conf = 0.6 if detail["samples"] else 0.0
    if detail["samples"]:
        hits = sum([detail["og_title"], detail["og_image"], bool(detail["title_selector"])])
        detail_conf = 0.4 + 0.2 * hits
    pag_conf = 0.9 if pagination["style"] in ("page_path", "number_suffix", "query") else 0.2
    return {
        "card_selector": _grade(card_conf),
        "link": _grade(link_conf),
        "unique_links": _grade(unique_conf),
        "poster": _grade(poster_conf),
        "search": _grade(search_conf),
        "detail": _grade(detail_conf),
        "pagination": _grade(pag_conf),
    }


def build_report(profile: dict) -> str:
    L = []
    mark = {"high": "[+]", "medium": "[~]", "low": "[!]", "none": "[-]"}
    conf = profile.get("confidence", {})

    L.append("=" * 62)
    L.append(f"SITE PROFILE — {profile['site']['name']}  ({profile['site']['main_url']})")
    L.append("=" * 62)

    lis = profile["listing"]
    if lis.get("card_selector"):
        L.append(f"{mark[conf.get('card_selector','low')]} card selector   : {lis['card_selector']}  ({lis['card_count']} cards, {lis['link_count']} links)")
        L.append(f"{mark[conf.get('link','low')]} link selector   : {lis['link']['selector'] or '(card is the link)'}")
        L.append(f"{mark[conf.get('unique_links','low')]} unique links    : {lis['unique_link_count']} distinct detail URLs")
        L.append(f"{mark[conf.get('poster','low')]} poster          : {lis['poster']['selector']} @ {lis['poster']['attr'] or 'n/a'}")
    else:
        L.append("[!] NO card pattern detected — site may be JS-rendered or API-driven.")
    L.append(f"{mark[conf.get('pagination','low')]} pagination      : {lis['pagination']['style']}"
             + (f"  ->  {lis['pagination']['template']}" if lis['pagination']['template'] else ""))
    search = profile.get("search", {}) or {}
    L.append(f"{mark[conf.get('search','low')]} search          : "
             + (search.get("url_template") or "not found (implement manually)") )
    L.append(f"[#] sections       : {len(lis['sections'])}")
    for s in lis["sections"]:
        L.append(f"      - {s['name']:<24} {s['url']}")

    det = profile["detail"]
    if det["samples"]:
        L.append(f"{mark[conf.get('detail','low')]} detail page     : og:title={det['og_title']} og:image={det['og_image']} h1={det['title_selector']}")
        L.append(f"      description   : {det['desc_selector'] or '(og/meta fallback)'}")
        L.append(f"      links box     : {det['detail_links_selector'] or 'a[href] (whole page, filtered)'}")
        L.append(f"      stream hosts  : {', '.join(det['stream_hosts']) or '(none detected)'}")
        if det["iframes"]:
            L.append(f"      iframes       : {', '.join(host_of(u) for u in det['iframes'])}")
    else:
        L.append("[!] detail pages unreachable — selectors will use og: meta fallback.")

    vid = profile["video"]
    if vid.get("scanned"):
        L.append(f"[v] video scan     : mode={vid['mode']}")
        for u in vid.get("found_m3u8", [])[:3]:
            L.append(f"      m3u8 : {u[:90]}")
        for u in vid.get("found_iframes", [])[:3]:
            L.append(f"      iframe : {u[:90]}")
        L.append(f"      note : {vid['notes']}")
    else:
        L.append("[-] video scan    : skipped")

    L.append("=" * 62)
    review = [k for k, v in conf.items() if v in ("low",)]
    if review:
        L.append("Review before generating: " + ", ".join(review))
    L.append("=" * 62)
    return "\n".join(L)


# ----------------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------------

def _derive_name(url: str) -> str:
    host = host_of(url)
    parts = host.split(".")
    name = parts[-2] if len(parts) >= 2 else host
    return "".join(w.capitalize() for w in re.split(r"[-_0-9]+", name) if w)


def _section_name_from_url(url: str) -> str:
    """Human name for a section URL: /category/movies/ -> Movies."""
    path = urlsplit(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    structural = {"category", "categories", "c", "tag", "tags", "page", "p"}
    meaningful = [p for p in parts if p not in structural and not p.isdigit()]
    if meaningful:
        return meaningful[-1].replace("-", " ").replace("_", " ").title()[:32]
    return _derive_name(url)


def profile_site(
    url: str,
    section_urls: list | None = None,
    video_url: str | None = None,
    discover: bool = False,
    max_sections: int = 6,
    min_cards: int = 5,
    timeout: int = 20,
    fetcher=None,
    scan_search: bool = True,
    scan_video: bool = True,
):
    """Full stage-1 scan. Returns the profile dict."""
    fetcher = fetcher or default_fetcher
    url = url.strip()
    html, info = fetcher(url)
    if not html:
        raise ProfilerError(
            f"Could not fetch {url} (status={info.get('status', 0)}, "
            f"error={info.get('error', 'n/a')}). "
            + ("Site looks Cloudflare-protected — install cloudscraper: pip install cloudscraper"
               if info.get("cloudflare") else "")
        )
    soup = parse_html(html)
    main_url = origin_of(url)
    base_host = host_of(url)

    # ---- cards ----
    card_selector, cards, card_stats, card_score = detect_cards(soup, base_host, min_cards)

    link_selector, self_link, hrefs = detect_link_selector(cards, base_host) if cards else (None, False, [])
    title_selector, title_method = (detect_title_selector(cards, link_selector, self_link, base_host)
                                    if cards else (None, "link_text"))
    poster_selector, poster_attr, poster_count = detect_poster(cards) if cards else (None, None, 0)

    unique_hrefs = [urljoin(main_url, h) for h in hrefs if h]
    unique_hrefs = [u for u in dict.fromkeys(unique_hrefs)
                    if re.match(r"^https?://", u) and host_of(u) == base_host]
    detail_url_prefixes = []
    if unique_hrefs:
        path_prefixes = Counter()
        for u in unique_hrefs:
            parts = urlsplit(u).path.strip("/").split("/")
            if parts and parts[0]:
                path_prefixes["/" + parts[0] + "/"] += 1
        detail_url_prefixes = [p for p, c in path_prefixes.most_common(3) if c >= len(unique_hrefs) * 0.6]

    pagination = detect_pagination(soup, url)

    # ---- search ----
    sample_query = "test"
    title_probe = None
    if cards:
        t_el = cards[0].find("img")
        if t_el is not None and t_el.get("alt"):
            title_probe = t_el["alt"].strip()[:24]
    if title_probe:
        sample_query = title_probe
    search = (detect_search(soup, url, card_selector, sample_query, fetcher, timeout)
              if scan_search else {"found": False, "url_template": None, "verified": False})

    # ---- sections ----
    sections = [{"name": "Home", "url": url}]
    if discover and card_selector:
        for sec in discover_sections(soup, url, card_selector, fetcher,
                                      max_sections=max_sections, min_cards=3,
                                      timeout=timeout):
            sections.append(sec)
    for extra in section_urls or []:
        if normalize_url(extra) == normalize_url(url):
            continue
        ehtml, _ = fetcher(extra, timeout=timeout)
        ok = False
        if ehtml and card_selector:
            ok = len(parse_html(ehtml).select(card_selector)) >= min(3, min_cards)
        elif ehtml:
            ok = True
        if ok:
            sections.append({"name": _section_name_from_url(extra) or f"Section {len(sections) + 1}",
                             "url": extra.strip()})
        else:
            print(f"[!] section skipped (no cards found): {extra}", file=sys.stderr)

    # ---- details ----
    detail_urls = unique_hrefs[:2]
    detail = (profile_details(detail_urls, url, fetcher, timeout=timeout)
              if detail_urls else
              {"samples": [], "title_selector": None, "desc_selector": None,
               "og_title": False, "og_image": False, "og_description": False,
               "meta_description": False, "stream_hosts": [], "detail_links_selector": None,
               "episode_links_detected": False, "iframes": []})

    # ---- video ----
    video_target = video_url
    if not video_target and scan_video:
        if detail["stream_hosts"] and detail["samples"]:
            # re-read the first detail page and grab the first external
            # (non-junk) link — that is the player page on most sites
            dhtml, _ = fetcher(detail["samples"][0], referer=url, timeout=timeout)
            if dhtml:
                dsoup = parse_html(dhtml)
                for a in dsoup.find_all("a", href=True):
                    h = a["href"].strip()
                    if (h.startswith("http") and host_of(h) != base_host
                            and not _skip_href(h)
                            and not any(j in host_of(h) for j in JUNK_HOSTS)):
                        video_target = h
                        break
        elif detail["iframes"]:
            video_target = detail["iframes"][0]
    video = (profile_video_page(video_target, detail["samples"][0] if detail["samples"] else url,
                                fetcher, timeout=timeout)
             if scan_video and video_target else
             {"scanned": False, "sample_url": None, "mode": "unknown", "found_m3u8": [],
              "found_mp4": [], "found_iframes": [], "packed_script": False,
              "player_hints": [], "notes": "not scanned"})

    profile = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": {
            "main_url": main_url,
            "name": _derive_name(url),
            "language": "en",
            "icon_url": None,
            "cloudflare_protected": bool(info.get("cloudflare")),
            "user_agent": USER_AGENT,
        },
        "listing": {
            "sections": sections,
            "card_selector": card_selector,
            "card_count": len(cards),
            "link_count": len(hrefs),
            "unique_link_count": len(unique_hrefs),
            "link": {"selector": link_selector, "self": self_link, "attr": "href"},
            "title": {"selector": title_selector, "method": title_method},
            "poster": {"selector": poster_selector, "attr": poster_attr,
                       "count": poster_count},
            "pagination": pagination,
            "detail_url_prefixes": detail_url_prefixes,
            "card_stats": card_stats,
            "card_score": round(card_score, 1),
        },
        "detail": detail,
        "search": search,
        "video": video,
    }
    profile["confidence"] = build_confidence(card_score, card_stats,
                                             profile["listing"]["poster"],
                                             search, detail, pagination)
    return profile


# ----------------------------------------------------------------------------
# standalone CLI
# ----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="AutoCloudStream stage 1 — site profiler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", required=True, help="listing page URL of the site")
    ap.add_argument("--section-url", action="append", default=[],
                    help="extra listing URL to add as home-page section (repeatable)")
    ap.add_argument("--video-url", help="player/embed URL to scan (optional)")
    ap.add_argument("--discover", action="store_true",
                    help="auto-discover extra sections from nav/category links")
    ap.add_argument("--max-sections", type=int, default=6)
    ap.add_argument("--min-cards", type=int, default=5,
                    help="min repetitions for a card pattern (default 5)")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--no-search", action="store_true", help="skip search detection")
    ap.add_argument("--no-video", action="store_true", help="skip video page scan")
    ap.add_argument("-o", "--output", default="profile.json", help="output JSON path")
    ap.add_argument("--quiet", action="store_true", help="only write the JSON")
    args = ap.parse_args(argv)

    try:
        profile = profile_site(
            url=args.url,
            section_urls=args.section_url,
            video_url=args.video_url,
            discover=args.discover,
            max_sections=args.max_sections,
            min_cards=args.min_cards,
            timeout=args.timeout,
            scan_search=not args.no_search,
            scan_video=not args.no_video,
        )
    except ProfilerError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    if not args.quiet:
        print(build_report(profile))
        print(f"[>] profile written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
