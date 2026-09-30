# AutoCloudStream

**Auto-generate [CloudStream](https://github.com/recloudstream/cloudstream) providers (.kt) from any streaming website.**

Point it at a site's listing page and it scans the HTML, detects the site's
structure, and emits a ready-to-build CloudStream extension module — same
pattern as the providers in [devfahim00/csdev](https://github.com/devfahim00/csdev)
(HdHub / Ctghall / DesiTales).

```
+-------------------+     +----------------------+     +--------------------------+
|   site_profiler   |     |      templates       |     |    generate_provider     |
|  (stage 1)        |     |  (stage 2)           |     |  (stage 3)               |
|                   |     |                      |     |                          |
| listing page ---> |     | provider .kt.tpl     |     | profile.json ----------> |
|  - card pattern   | --> | plugin .kt.tpl       | --> |  - fill selectors        |
|  - selectors      |     | build.gradle.tpl     |     |  - pagination style      |
|  - pagination     |     |                      |     |  - search / stream hosts |
|  - search form    |     | generic CloudStream  |     |        |                 |
|  - stream hosts   |     | MainAPI skeleton     |     |        v                 |
|  - m3u8 scan      |     | with {{placeholders}}|     |  <Site>/                 |
|                   |     |                      |     |   build.gradle.kts       |
| output:           |     |                      |     |   src/main/kotlin/...    |
|  profile.json     |     |                      |     |     <Site>Provider.kt    |
+-------------------+     +----------------------+     |     <Site>Plugin.kt     |
                                                       +--------------------------+
```

**What is automated:** home page sections, card → search-result mapping
(title / poster / link selectors), pagination, search endpoint, detail page
parsing (og: meta + on-page selectors), stream-host detection, and a generic
best-effort `loadLinks()` extractor (direct m3u8/mp4 → regex scan →
p.a.c.k.e.r unpacker → WebViewResolver → CloudStream `loadExtractor()`).

**What stays manual:** per-site video extraction when the player is
token-protected or heavily obfuscated — every site does this differently, so
the generated provider ships a clearly marked `MANUAL REVIEW` block with the
profiler's findings to make that step as small as possible.

---

## Install

```bash
pip install -r requirements.txt
# optional: helps with Cloudflare-protected sites
pip install cloudscraper
```

Requires Python 3.10+.

## Quickstart

```bash
# 1. Scan a site and generate the provider module in one shot:
python main.py auto --url https://example.com/movies/ --discover

# 2. The output lands in ./output/<SiteName>/
#    copy that folder into your CloudStream plugins repo (csdev) and build.
```

Or stage by stage:

```bash
# stage 1: profile the site (writes profile.json + prints a report)
python site_profiler.py --url https://example.com/movies/ -o profile.json

# stage 3: turn the profile into a provider module
python generate_provider.py --profile profile.json --out output/
```

## GitHub Actions — one-click generation (no PC needed)

The repo ships with a **Generate Provider** workflow. Open
**Actions → Generate Provider → Run workflow** and it *asks for the
website URL*, then does everything automatically:

1. **Profiles** the site (`profiles/<Site>.json` — committed)
2. **Generates** the full provider module (`plugins-repo/<Site>/` — committed)
3. **Builds** the `.cs3` plugin with Gradle (same setup as the csdev repo)
4. **Uploads** the `.cs3` as a run artifact and commits it to `builds/`
   along with a ready-to-use `plugins.json`

Workflow inputs:

| input | description |
|-------|-------------|
| `url` | website listing page URL (required) — the workflow prompt |
| `name` | provider display name (optional, auto from domain) |
| `discover` | auto-discover extra home sections (default true) |
| `telegram_url` | promo channel (empty = disable the promo block) |
| `build_cs3` | also compile the `.cs3` plugin (default true) |

The profile report (detected selectors, pagination, stream hosts, video
findings) is printed in the run's **Summary** page, so you can review what
was detected without leaving GitHub. If the selectors look wrong, edit
them in `plugins-repo/<Site>/.../Provider.kt` (companion object at the
bottom) and re-run **Build All Plugins**.

Then in CloudStream: *Settings → Extensions → Add repository* and paste

```
https://raw.githubusercontent.com/devfahim00/AutoCloudStream/main/builds/plugins.json
```

There is also a **Build All Plugins** workflow (no inputs) that rebuilds
every `.cs3` in `plugins-repo/` after you hand-edit a provider.

## CLI reference

### `python main.py auto` (or `site_profiler.py`) — profiling

| flag | description |
|------|-------------|
| `--url URL` | listing page to scan (required) |
| `--section-url URL` | extra listing page to add as a home section (repeatable) |
| `--discover` | auto-discover extra sections from nav/category links |
| `--max-sections N` | cap discovered sections (default 6) |
| `--min-cards N` | min repetitions for a card pattern (default 5) |
| `--video-url URL` | player/embed URL to scan (auto-discovered if omitted) |
| `--no-search` / `--no-video` | skip search / video probing |
| `--timeout N` | HTTP timeout in seconds (default 20) |
| `-o FILE` | profile JSON path (default `profile.json`) |

### `python main.py generate` (or `generate_provider.py`) — generation

| flag | description |
|------|-------------|
| `--profile FILE` | profile JSON from stage 1 (required) |
| `--out DIR` | output directory (default `output`) |
| `--package PKG` | Kotlin package, e.g. `com.mysite` (default `com.<site>`) |
| `--class-name NAME` | provider class (default `<Site>Provider`) |
| `--name NAME` | display name (default from profile) |
| `--author NAME` | build.gradle.kts author (default `devfahim00`) |
| `--lang CODE` | provider language (default from profile) |
| `--status N` | 0 Down / 1 Ok / 2 Slow / 3 Beta (default 3) |
| `--icon URL` | icon URL (default `<mainUrl>/favicon.ico`) |
| `--series` | force `TvType.TvSeries` into supportedTypes |
| `--telegram-url URL` | telegram promo channel — same block as your other providers (empty string disables) |
| `--single` | emit only `Provider.kt` instead of the full module |

## Profile JSON

The profiler's output is a plain JSON document — commit it next to your
provider so you can regenerate after tweaking selectors:

```jsonc
{
  "schema_version": 1,
  "site":   { "main_url": "...", "name": "...", "language": "en" },
  "listing": {
    "sections":  [{ "name": "Home", "url": "..." }],
    "card_selector": "li.thumb",          // repeated card element
    "link":   { "selector": "a[href]" },  // detail link inside a card
    "title":  { "selector": "figcaption p.title" },
    "poster": { "selector": "a img", "attr": "data-src" },
    "pagination": { "style": "page_path", "template": ".../page/{page}/" },
    "detail_url_prefixes": ["/movies/"]
  },
  "detail": {
    "title_selector": "h1.page-title",
    "desc_selector": ".description",
    "detail_links_selector": ".page-body a[href]",
    "stream_hosts": ["watch.examplehost.com"]
  },
  "search": { "url_template": "https://example.com/?s={query}" },
  "video":  { "mode": "direct_m3u8|packed|iframe|unknown", "found_m3u8": [] },
  "confidence": { "card_selector": "high", "search": "medium", "...": "..." }
}
```

Every field is documented by example in [`examples/profile.json`](examples/profile.json).

## The generated provider

```
output/<Site>/
├── build.gradle.kts              # cloudstream { authors, status, tvTypes, iconUrl }
└── src/main/
    ├── AndroidManifest.xml
    └── kotlin/com/<site>/
        ├── <Site>Plugin.kt       # @CloudstreamPlugin entry point
        └── <Site>Provider.kt     # MainAPI implementation
```

`<Site>Provider.kt` contains:

- `mainPage` / `getMainPage` — detected sections + pagination style
- `search` — detected endpoint (or a TODO stub)
- `load` — og:meta first, detected selectors as fallback; collects playable
  links by stream host and packs them `label@@@url|||...` (HdHub format);
  auto-detects series pages by keyword
- `loadLinks` — generic chain: direct m3u8/mp4 → `extractStatic` (regex +
  p.a.c.k.e.r unpacker) → `extractViaWebView` (WebViewResolver intercept) →
  `loadExtractor` (built-in CloudStream extractors for dood/streamtape/...)
- a **MANUAL REVIEW** banner with every profiler finding for the player page
- the Telegram promo block (same as HdHub/DesiTales) — disable with
  `--telegram-url ""`
- all selectors as constants at the bottom (companion object) — tweak there

Drop the module folder into your plugins repo; `settings.gradle.kts` picks
up any folder with a `build.gradle.kts` automatically (csdev-style repos).

## Examples

[`examples/`](examples/) contains a complete run against a fixture site
(`tests/fixtures/`): the [`profile.json`](examples/profile.json) and the
resulting module in [`examples/generated/`](examples/generated/).

## Tests

```bash
python -m unittest discover -s tests -v   # offline, no network needed
```

## Limitations

- JS-rendered sites (React/Vue SPAs that fetch cards via XHR) can't be
  profiled from static HTML — the profiler will report "no card pattern".
  API-driven sites like Ctghall need a hand-written provider.
- Cloudflare-protected sites may need `pip install cloudscraper` (used
  automatically when a 403/503 challenge is detected).
- Video extraction stays best-effort: token-rotating m3u8s, DRM and
  heavily obfuscated players are per-site work by design.

## License

MIT — see [LICENSE](LICENSE).
