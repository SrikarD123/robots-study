#!/usr/bin/env python3
"""Download the archived history of each site's robots.txt from the Wayback Machine.

    python fetch.py sites.csv                  # every site in the list
    python fetch.py sites.csv --only npr.org   # one site (use this for a test run)

For each site this asks the Wayback CDX API for every capture of /robots.txt
whose content differs from the capture before it (collapse=digest), so the list
is the file's change history rather than thousands of identical copies. Before
--dense-from it keeps one version per year (the last change seen that year, i.e.
the file as it stood at year's end). From --dense-from onward, when the AI-crawler
rules appear, it keeps every change, or, if there are more than --max-dense, one
version from each of --max-dense equal stretches of time (about every ten days).

Each kept version is downloaded raw through the id_ endpoint (no Wayback link
rewriting), checked to be a real robots.txt rather than a saved error page, and
written to data/raw/<domain>/<timestamp>.txt. Rejected captures leave a
<timestamp>.rejected marker so a re-run does not fetch them again. Every capture
considered is listed in data/manifest.csv with what happened to it.

Re-running is safe and cheap: anything already on disk is skipped, so if the
script is interrupted, just run the same command again.
"""
from __future__ import annotations

import argparse
import csv
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package:  pip install requests")

CDX_URL = "https://web.archive.org/cdx/search/cdx"
RAW_URL = "https://web.archive.org/web/{ts}id_/{url}"
VIEW_URL = "https://web.archive.org/web/{ts}/{url}"

# RFC 9309 asks crawlers to identify themselves; this script should too.
# If you want to run this script yourself, replace YOUR_EMAIL below with your
# own email address so archive operators can contact you.
USER_AGENT = "robots-txt-history-study/1.0 (NC State E497 capstone; contact: YOUR_EMAIL)"

DIRECTIVE = re.compile(r"^\s*(user-agent|disallow|allow|sitemap|crawl-delay)\s*:", re.I | re.M)


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(session: requests.Session, url: str, params: dict | None, delay: float,
             tries: int = 6) -> requests.Response:
    """GET with polite pacing and exponential backoff on rate limits and server errors."""
    for attempt in range(tries):
        time.sleep(delay)
        try:
            r = session.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            wait = min(120, 4 * 2 ** attempt) + random.random()
            log(f"    network error ({e.__class__.__name__}); retrying in {wait:.0f}s")
            time.sleep(wait)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = min(300, 10 * 2 ** attempt) + random.random()
            log(f"    archive returned {r.status_code}; backing off {wait:.0f}s")
            time.sleep(wait)
            continue
        return r
    raise RuntimeError(f"gave up after {tries} attempts: {url}")


def list_captures(session, host: str, from_year: int, to_year: int, delay: float) -> list[dict]:
    params = {
        "url": f"{host}/robots.txt",
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest,length",
        "filter": "statuscode:200",
        "collapse": "digest",
        "from": str(from_year),
        "to": str(to_year),
    }
    r = http_get(session, CDX_URL, params, delay)
    r.raise_for_status()
    if not r.text.strip():
        return []
    rows = r.json()
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    return [dict(zip(header, row)) for row in body]


def _ts_to_dt(ts: str) -> datetime:
    return datetime.strptime(ts[:14].ljust(14, "0"), "%Y%m%d%H%M%S")


def select_versions(captures: list[dict], dense_from: str, max_dense: int,
                    cached: set[str] | frozenset = frozenset()) -> tuple[list[dict], list[dict]]:
    """Return (kept, thinned).

    Before dense_from: one version per year (the year's last change).
    From dense_from on: every change if there are at most max_dense of them;
    otherwise the period is cut into max_dense equal stretches of time and one
    version is kept from each stretch, so sampling is even in time rather than in
    count. (The archive captured some years far more often than others, so
    sampling by count leaves recent years nearly empty.) Within a stretch, an
    already-downloaded version is preferred, so re-runs fetch as little as
    possible. Versions already on disk are always kept.
    """
    captures = sorted(captures, key=lambda c: c["timestamp"])
    per_year: dict[str, dict] = {}
    dense: list[dict] = []
    for c in captures:
        if c["timestamp"] < dense_from:
            per_year[c["timestamp"][:4]] = c  # later captures overwrite: keeps year's last
        else:
            dense.append(c)
    chosen = {c["timestamp"] for c in per_year.values()}

    if not max_dense or len(dense) <= max_dense:
        chosen |= {c["timestamp"] for c in dense}
    else:
        start = _ts_to_dt(dense_from)
        end = _ts_to_dt(dense[-1]["timestamp"])
        span = max((end - start).total_seconds(), 1.0)
        bins: dict[int, list[dict]] = defaultdict(list)
        for c in dense:
            k = min(int((_ts_to_dt(c["timestamp"]) - start).total_seconds() / span * max_dense), max_dense - 1)
            bins[k].append(c)
        for members in bins.values():
            on_disk = [c for c in members if c["timestamp"] in cached]
            chosen.add((on_disk or members)[-1]["timestamp"])

    chosen |= {c["timestamp"] for c in captures if c["timestamp"] in cached}
    kept = [c for c in captures if c["timestamp"] in chosen]
    thinned = [c for c in captures if c["timestamp"] not in chosen]
    return kept, thinned


def validate(body: bytes) -> str:
    """Return '' if this looks like a real robots.txt, otherwise the reason it doesn't."""
    if not body.strip():
        return "empty file"
    text = body.decode("utf-8", errors="replace")
    head = text.lstrip()[:600].lower()
    if head.startswith("<") or "<html" in head or "<!doctype" in head or "<body" in head:
        return "HTML page (not robots.txt)"
    if not DIRECTIVE.search(text):
        return "no robots.txt directives found"
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sites", help="CSV with columns domain,category")
    ap.add_argument("--out", default="data", help="output folder (default: data)")
    ap.add_argument("--from-year", type=int, default=2015)
    ap.add_argument("--to-year", type=int, default=2026)
    ap.add_argument("--dense-from", default="2022-07-01",
                    help="keep every change from this date on (default 2022-07-01)")
    ap.add_argument("--max-dense", type=int, default=150,
                    help="cap on post-dense-from versions per site; 0 = no cap (default 150)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (default 1.5)")
    ap.add_argument("--only", help="fetch just this one domain")
    args = ap.parse_args()

    if "YOUR_EMAIL" in USER_AGENT:
        log("Note: edit USER_AGENT at the top of fetch.py to include your email before the full run.\n")

    dense_from = args.dense_from.replace("-", "")
    out = Path(args.out)
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    with open(args.sites, newline="") as f:
        sites = [row for row in csv.DictReader(f) if row.get("domain", "").strip()]
    if args.only:
        sites = [s for s in sites if s["domain"].strip() == args.only]
        if not sites:
            sys.exit(f"{args.only} is not in {args.sites}")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    manifest_path = out / "manifest.csv"
    existing: list[dict] = []
    if manifest_path.exists():
        with open(manifest_path, newline="") as f:
            existing = [r for r in csv.DictReader(f)]
    # Drop old rows for the sites we are about to (re)process.
    touched = {s["domain"].strip() for s in sites}
    manifest = [r for r in existing if r["domain"] not in touched]

    totals = {"kept": 0, "rejected": 0, "error": 0}
    for n, site in enumerate(sites, 1):
        domain, category = site["domain"].strip(), site.get("category", "").strip()
        log(f"[{n}/{len(sites)}] {domain}")
        site_dir = raw_dir / domain
        site_dir.mkdir(exist_ok=True)

        captures: list[dict] = []
        for host in (domain, f"www.{domain}"):
            try:
                captures = list_captures(session, host, args.from_year, args.to_year, args.delay)
            except Exception as e:  # noqa: BLE001 - report and move on to the next site
                log(f"    could not list captures for {host}: {e}")
                captures = []
            if captures:
                break
        if not captures:
            log("    no archived captures found; skipping")
            manifest.append({"domain": domain, "category": category, "timestamp": "",
                             "date": "", "status": "no captures", "reason": "",
                             "original_url": "", "wayback_url": ""})
            continue

        cached = {p.stem for p in site_dir.glob("*.txt")} | {p.stem for p in site_dir.glob("*.rejected")}
        kept, thinned = select_versions(captures, dense_from, args.max_dense, cached)
        new = sum(1 for c in kept if c["timestamp"] not in cached)
        log(f"    {len(captures)} distinct versions archived; keeping {len(kept)}, "
            f"{new} new to download" + (f" ({len(thinned)} thinned)" if thinned else ""))

        for c in thinned:
            manifest.append(row_for(domain, category, c, "thinned", "not selected by sampling rule"))

        for c in kept:
            ts = c["timestamp"]
            txt_path = site_dir / f"{ts}.txt"
            rej_path = site_dir / f"{ts}.rejected"
            if txt_path.exists():
                manifest.append(row_for(domain, category, c, "kept", "cached"))
                totals["kept"] += 1
                continue
            if rej_path.exists():
                manifest.append(row_for(domain, category, c, "rejected", rej_path.read_text().strip()))
                totals["rejected"] += 1
                continue
            try:
                r = http_get(session, RAW_URL.format(ts=ts, url=c["original"]), None, args.delay)
            except Exception as e:  # noqa: BLE001
                log(f"    {ts}: fetch failed ({e})")
                manifest.append(row_for(domain, category, c, "error", str(e)))
                totals["error"] += 1
                continue
            if r.status_code != 200:
                reason = f"HTTP {r.status_code} from archive"
                rej_path.write_text(reason)
                manifest.append(row_for(domain, category, c, "rejected", reason))
                totals["rejected"] += 1
                continue
            reason = validate(r.content)
            if reason:
                rej_path.write_text(reason)
                manifest.append(row_for(domain, category, c, "rejected", reason))
                totals["rejected"] += 1
            else:
                txt_path.write_bytes(r.content)
                manifest.append(row_for(domain, category, c, "kept", ""))
                totals["kept"] += 1

        write_manifest(manifest_path, manifest)  # save progress after every site

    write_manifest(manifest_path, manifest)
    log(f"\nDone. kept {totals['kept']}, rejected {totals['rejected']}, errors {totals['error']}.")
    log(f"Files are in {raw_dir}/, and the full record is {manifest_path}.")
    if totals["error"]:
        log("Some downloads failed. Run the same command again to retry just those.")


def row_for(domain: str, category: str, c: dict, status: str, reason: str) -> dict:
    ts = c["timestamp"]
    return {
        "domain": domain,
        "category": category,
        "timestamp": ts,
        "date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}",
        "status": status,
        "reason": reason,
        "original_url": c["original"],
        "wayback_url": VIEW_URL.format(ts=ts, url=c["original"]),
    }


def write_manifest(path: Path, rows: list[dict]) -> None:
    fields = ["domain", "category", "timestamp", "date", "status", "reason", "original_url", "wayback_url"]
    rows = sorted(rows, key=lambda r: (r["domain"], r["timestamp"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Run the same command again to pick up where it left off.")
