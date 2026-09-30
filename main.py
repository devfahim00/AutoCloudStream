#!/usr/bin/env python3
"""
AutoCloudStream — CLI entry point
=================================

One command to turn a streaming site into a CloudStream provider:

    # full pipeline: scan site -> generate module
    python main.py auto --url https://example.com/movies --discover

    # stage by stage
    python main.py profile --url https://example.com/movies -o profile.json
    python main.py generate --profile profile.json

Architecture (3 stages, same as your original design):

    [stage 1]  site_profiler.py     site scan -> profile.json
    [stage 2]  templates/*.kt.tpl   generic CloudStream MainAPI skeleton
    [stage 3]  generate_provider.py profile + template -> .kt module

The video-link extraction stays semi-manual (m3u8 tokens / JS-obfuscated
players differ per site) — the generated provider ships a generic
best-effort extractor + a clearly marked MANUAL REVIEW block.
"""
from __future__ import annotations

import argparse
import json
import sys

import generate_provider as gen
import site_profiler as prof


def _add_profile_args(ap):
    ap.add_argument("--url", required=True, help="listing page URL of the site")
    ap.add_argument("--section-url", action="append", default=[],
                    help="extra listing URL to add as a home section (repeatable)")
    ap.add_argument("--video-url", help="player/embed URL to scan (optional)")
    ap.add_argument("--discover", action="store_true",
                    help="auto-discover extra sections from nav/category links")
    ap.add_argument("--max-sections", type=int, default=6)
    ap.add_argument("--min-cards", type=int, default=5,
                    help="min repetitions for a card pattern (default 5)")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--no-search", action="store_true", help="skip search detection")
    ap.add_argument("--no-video", action="store_true", help="skip video page scan")
    ap.add_argument("-o", "--output", default="profile.json",
                    help="profile JSON path (default ./profile.json)")


def _add_generate_args(ap):
    ap.add_argument("--profile", help="site profile JSON (stage 3 input)")
    ap.add_argument("--out", default="output", help="output directory (default ./output)")
    ap.add_argument("--package", help="Kotlin package, e.g. com.mysite")
    ap.add_argument("--class-name", help="provider class name (default <Site>Provider)")
    ap.add_argument("--name", help="provider display name")
    ap.add_argument("--author", default=gen.DEFAULT_AUTHOR)
    ap.add_argument("--lang", help="language code (default from profile)")
    ap.add_argument("--description", default=gen.DEFAULT_DESCRIPTION)
    ap.add_argument("--status", type=int, default=3,
                    help="plugin status: 0 Down / 1 Ok / 2 Slow / 3 Beta (default 3)")
    ap.add_argument("--icon", help="icon URL (default <mainUrl>/favicon.ico)")
    ap.add_argument("--series", action="store_true", help="force TvSeries support")
    ap.add_argument("--telegram-url", default=gen.DEFAULT_TELEGRAM_URL,
                    help="telegram promo channel (empty string disables the block)")
    ap.add_argument("--telegram-poster", default=gen.DEFAULT_TELEGRAM_POSTER)
    ap.add_argument("--single", action="store_true",
                    help="emit only the Provider.kt file (no module layout)")


def cmd_profile(args):
    try:
        profile = prof.profile_site(
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
    except prof.ProfilerError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    print(prof.build_report(profile))
    print(f"[>] profile written to {args.output}")
    print(f"[i] next: python main.py generate --profile {args.output}")
    return 0


def _run_generate(profile, args):
    args.profile_path = args.profile or args.output
    if getattr(args, "telegram_url", None) is not None and args.telegram_url.strip() == "":
        args.telegram_url = None
    try:
        files = gen.generate(profile, args)
    except gen.GeneratorError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return None
    package = args.package or f"com.{gen.slug(args.name or profile['site'].get('name') or 'site')}"
    written = gen.write_outputs(files, args, package)
    print("[>] generated provider:")
    for path in written:
        print(f"      {path}")
    print("[i] review checklist:")
    print("      1. selectors in the companion object (bottom of Provider.kt)")
    print("      2. the MANUAL REVIEW block in loadLinks() — per-site video logic")
    print("      3. build.gradle.kts metadata (name/description/status/icon)")
    print("      4. copy the module folder into your plugins repo (csdev)")
    return written


def cmd_generate(args):
    profile_path = args.profile or "profile.json"
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)
    except OSError as exc:
        print(f"[x] cannot read profile: {exc}", file=sys.stderr)
        return 2
    written = _run_generate(profile, args)
    return 0 if written else 2


def cmd_auto(args):
    # ---- stage 1 ----
    try:
        profile = prof.profile_site(
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
    except prof.ProfilerError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 2
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    print(prof.build_report(profile))
    print(f"[>] profile written to {args.output}")

    # ---- stage 3 ----
    if not args.name:
        args.name = profile["site"].get("name")
    args.profile = args.output
    written = _run_generate(profile, args)
    return 0 if written else 2


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="autocs",
        description="AutoCloudStream — CloudStream provider auto-generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Architecture")[0] if __doc__ else None,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("profile", help="stage 1 — scan a site into profile.json")
    _add_profile_args(p1)
    p1.set_defaults(func=cmd_profile)

    p2 = sub.add_parser("generate", help="stage 3 — profile.json -> .kt module")
    _add_generate_args(p2)
    p2.set_defaults(func=cmd_generate)

    p3 = sub.add_parser("auto", help="stage 1 + 3 in one shot")
    _add_profile_args(p3)
    _add_generate_args(p3)
    p3.set_defaults(func=cmd_auto)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
