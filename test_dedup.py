#!/usr/bin/env python3
"""
Semantic dedup calibration tool — shows both dedup layers side by side.

Layer 2: title similarity against 90-day article cache  (no external calls)
Layer 3: MemPalace semantic search against past intel reports

No Tavily calls. No LLM calls. No writes anywhere. Safe to run repeatedly.

Usage:
  python test_dedup.py
  python test_dedup.py --companies TCL,海尔集团,安踏集团
  python test_dedup.py --cutoff 2026-04-24          # only articles after this date
  python test_dedup.py --threshold 0.55,0.60,0.65,0.70
  python test_dedup.py --source-contains china-companies   # default
  python test_dedup.py --source-contains ""               # search all MemPalace
  python test_dedup.py --verbose                           # show matching snippet
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Import dedup_utils from same directory
sys.path.insert(0, str(Path(__file__).parent))
from dedup_utils import find_cache_duplicate, search_mempalace

CACHE_PATH = Path(__file__).resolve().parent / "data" / "article_cache.json"
DEFAULT_BRIDGE = "http://localhost:8765"   # host-side URL (not container)
DEFAULT_COMPANIES = ["TCL", "海尔集团", "安踏集团"]
DEFAULT_MP_THRESHOLDS = [0.55, 0.60, 0.65]
DEFAULT_CACHE_THRESHOLD = 0.45


# ── data ──────────────────────────────────────────────────────────────────────

def load_cache(companies: list[str], cutoff_ts: float | None) -> dict[str, list]:
    data = json.loads(CACHE_PATH.read_text())
    result: dict[str, list] = {}
    skipped = 0
    for url, entry in data.items():
        c = entry.get("company", "")
        if c not in companies:
            continue
        ts = entry.get("ts", 0)
        if cutoff_ts is not None and ts <= cutoff_ts:
            skipped += 1
            continue
        result.setdefault(c, []).append({"url": url, **entry})
    for c in result:
        result[c].sort(key=lambda x: x["ts"], reverse=True)
    if skipped:
        print(f"  (skipped {skipped} articles before cutoff)\n")
    return result


# ── bridge ────────────────────────────────────────────────────────────────────

def check_bridge(bridge_url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{bridge_url}/health", timeout=3) as r:
            return json.loads(r.read())
    except urllib.error.URLError:
        return None


# ── display ───────────────────────────────────────────────────────────────────

def print_threshold_summary(label: str, sims: list[float], thresholds: list[float]):
    total = len(sims)
    if not total:
        return
    print(f"  Threshold summary — {label}:")
    for t in thresholds:
        skip = sum(1 for s in sims if s >= t)
        keep = total - skip
        pct = skip / total * 100
        bar = "░" * keep + "▓" * skip
        print(f"    {t:.2f}  [{bar}]  KEEP {keep:2d} / SKIP {skip:2d}  ({pct:.0f}% filtered)")


def print_distribution(sims: list[float], label: str = ""):
    buckets = [(0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6),
               (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]
    if label:
        print(f"  Distribution ({label}):")
    for lo, hi in buckets:
        count = sum(1 for s in sims if lo <= s < hi)
        if count == 0 and lo < 0.3:
            continue
        bar = "█" * count
        lbl = f"{lo:.1f}–{hi:.1f}" if hi < 1.01 else f"{lo:.1f}–1.0"
        print(f"    {lbl}  {bar} {count}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dedup calibration — no side effects")
    parser.add_argument("--companies", default=",".join(DEFAULT_COMPANIES))
    parser.add_argument("--cutoff", default=None, metavar="YYYY-MM-DD",
                        help="Only test articles fetched after this date")
    parser.add_argument("--mp-threshold", default=",".join(str(t) for t in DEFAULT_MP_THRESHOLDS),
                        help="MemPalace similarity thresholds (default: 0.55,0.60,0.65)")
    parser.add_argument("--cache-threshold", type=float, default=DEFAULT_CACHE_THRESHOLD,
                        help="Title Jaccard threshold for cache dedup (default: 0.45)")
    parser.add_argument("--source-contains", default="china-companies",
                        help="Restrict MemPalace results to files containing this string "
                             "(default: china-companies). Pass empty string to search all.")
    parser.add_argument("--wing", default="paperview")
    parser.add_argument("--room", default="general")
    parser.add_argument("--n-results", type=int, default=3)
    parser.add_argument("--bridge", default=DEFAULT_BRIDGE)
    parser.add_argument("--exclude-report-date", default=None, metavar="YYYY-MM-DD",
                        help="Drop L3 hits whose source_file contains this date string "
                             "(use to exclude today's report from self-matching, "
                             "e.g. --exclude-report-date 2026-04-25)")
    parser.add_argument("--skip-mempalace", action="store_true",
                        help="Skip MemPalace queries (Layer 3) — faster, cache-only run")
    parser.add_argument("--verbose", action="store_true",
                        help="Show matching snippet / title for each article")
    args = parser.parse_args()

    companies = [c.strip() for c in args.companies.split(",")]
    mp_thresholds = sorted(float(t) for t in args.mp_threshold.split(","))
    source_contains = args.source_contains or None
    exclude_report_date: str | None = args.exclude_report_date or None
    cutoff_ts = (
        datetime.strptime(args.cutoff, "%Y-%m-%d").timestamp()
        if args.cutoff else None
    )

    # ── preflight ─────────────────────────────────────────────────────────────
    bridge_ok = False
    if not args.skip_mempalace:
        health = check_bridge(args.bridge)
        if health:
            bridge_ok = True
            print(f"Bridge  : {args.bridge}  ✓  palace={health.get('palace','?')}")
        else:
            print(f"Bridge  : {args.bridge}  ✗  (MemPalace layer skipped)")

    print(f"Filter  : source_contains={source_contains!r}  wing={args.wing}  room={args.room}")
    print(f"Cutoff  : {args.cutoff or 'none'}")
    print(f"Cache Δ : Jaccard threshold = {args.cache_threshold}")
    print(f"MP Δ    : similarity thresholds = {mp_thresholds}")
    print(f"Companies: {', '.join(companies)}\n")

    articles = load_cache(companies, cutoff_ts)
    if not articles:
        print("No articles found.")
        sys.exit(0)

    all_cache_sims: list[float] = []
    all_mp_sims: list[float] = []

    for company in companies:
        arts = articles.get(company, [])
        if not arts:
            print(f"{'=' * 72}")
            print(f"  {company}  — no articles\n")
            continue

        # All cached articles for this company (including pre-cutoff) for Layer 2
        all_cached = [
            {"url": url, **entry}
            for url, entry in json.loads(CACHE_PATH.read_text()).items()
            if entry.get("company") == company
        ]

        print(f"{'=' * 72}")
        print(f"  {company}  ({len(arts)} articles to test)")
        print(f"{'=' * 72}")
        print(f"  {'title':<55}  {'L2-cache':>8}  {'L3-mp':>6}")
        print(f"  {'-'*55}  {'-'*8}  {'-'*6}")

        c_sims: list[float] = []
        m_sims: list[float] = []

        for art in arts:
            url = art.get("url", "")
            title = (art.get("title") or "").strip()
            snippet = (art.get("content") or "")[:300]

            # Layer 2: title similarity against cache
            _, cache_score, cache_match = find_cache_duplicate(
                url, title, all_cached, threshold=args.cache_threshold
            )
            c_sims.append(cache_score)
            all_cache_sims.append(cache_score)

            c_flag = "▓" if cache_score >= args.cache_threshold else "░"

            # Layer 3: MemPalace
            if bridge_ok and not args.skip_mempalace:
                query = f"{title}. {snippet}"[:500]
                hits = search_mempalace(
                    query,
                    bridge_url=args.bridge,
                    wing=args.wing,
                    room=args.room,
                    source_contains=source_contains,
                    n_results=args.n_results,
                )
                # Drop self-matching hits (current report not yet mined in production)
                if exclude_report_date:
                    hits = [h for h in hits if exclude_report_date not in h.get("source_file", "")]
                mp_score = hits[0]["similarity"] if hits else 0.0
                mp_src = hits[0]["source_file"] if hits else "—"
                m_sims.append(mp_score)
                all_mp_sims.append(mp_score)
                mp_flag = "▓" if mp_score >= min(mp_thresholds) else "░"
                mp_col = f"{mp_flag}{mp_score:.3f}"
            else:
                mp_score, mp_src = 0.0, "—"
                mp_col = "  skip"

            title_short = title[:55] if title else "(no title)"
            print(f"  {title_short:<55}  {c_flag}{cache_score:.3f}    {mp_col}")

            if args.verbose:
                if cache_score >= args.cache_threshold:
                    print(f"    L2 match: {cache_match[:70]}")
                if mp_score >= min(mp_thresholds if mp_thresholds else [1.0]):
                    print(f"    L3 match: {mp_src}  sim={mp_score:.3f}")

        print()
        print_threshold_summary(f"{company} — cache (L2)", c_sims, [args.cache_threshold])
        if m_sims:
            print_threshold_summary(f"{company} — MemPalace (L3)", m_sims, mp_thresholds)
        print_distribution(c_sims, "cache Jaccard")
        if m_sims:
            print_distribution(m_sims, "MemPalace similarity")
        print()

    # ── overall ───────────────────────────────────────────────────────────────
    total = len(all_cache_sims)
    if total == 0:
        return

    print(f"{'=' * 72}")
    print(f"  OVERALL  ({total} articles, {len(companies)} companies)")
    print(f"{'=' * 72}")
    print_threshold_summary("cache (L2)", all_cache_sims, [args.cache_threshold])
    if all_mp_sims:
        print_threshold_summary("MemPalace (L3)", all_mp_sims, mp_thresholds)
    print()
    print_distribution(all_cache_sims, "cache Jaccard — all")
    if all_mp_sims:
        print_distribution(all_mp_sims, "MemPalace — all")
    print()

    # Combined: article skipped if EITHER layer flags it
    if all_mp_sims:
        for mp_t in mp_thresholds:
            combined_skip = sum(
                1 for cs, ms in zip(all_cache_sims, all_mp_sims)
                if cs >= args.cache_threshold or ms >= mp_t
            )
            combined_keep = total - combined_skip
            print(f"  Combined SKIP (L2≥{args.cache_threshold} OR L3≥{mp_t}): "
                  f"KEEP {combined_keep} / SKIP {combined_skip}  "
                  f"({combined_skip/total*100:.0f}% filtered)")


if __name__ == "__main__":
    main()
