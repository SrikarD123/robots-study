#!/usr/bin/env python3
"""Turn the downloaded robots.txt histories into tables you can write from.

    python analyze.py                 # reads data/, writes results/

Reads data/manifest.csv and data/raw/, plus bots.csv (the crawlers to track),
and writes to results/:

  site_summary.csv     one row per site: when it first mentioned an AI crawler,
                       when it first blocked one, what it blocks now, whether it
                       treats a company's training crawler differently from that
                       company's search crawler, and any reversals
  ai_changes.csv       every change between consecutive versions that affected
                       an AI crawler, with both Wayback URLs; this is where to
                       look for close-reading candidates
  snapshots.csv        every version of every file, with each tracked crawler's
                       status (blocked/allowed) and whether it is named
  first_block_timeline.csv
                       how many sites first blocked an AI crawler in each
                       half-year, by category
  diffs/<domain>.txt   line-by-line diffs between consecutive versions, with
                       changes that touch AI crawlers marked [AI]

How "blocked" is decided: each file is evaluated the way RFC 9309 says a crawler
should read it (see robots_parser.py) for one representative content path,
/news/2024/example-article by default (change it with --path). A crawler counts
as blocked if that path is disallowed for it.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from robots_parser import is_allowed, is_named, parse

DEFAULT_PATH = "/news/2024/example-article"


def load_bots(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        bots = [r for r in csv.DictReader(f) if r.get("token", "").strip()]
    for b in bots:
        b["token"] = b["token"].strip()
        b["ai"] = b.get("ai", "").strip().lower() in ("yes", "y", "true", "1")
    return bots


def _scheme_host(url: str) -> tuple[str, str]:
    u = urlsplit(url)
    return u.scheme.lower(), (u.hostname or "").rstrip(".").lower()


def load_snapshots(data: Path, dense_from: str, exclude: set[str]) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Load kept versions, restricted to each site's main URL.

    The archive files http://, https://, www. and bare-domain captures under one
    entry, but those hosts can serve different files (a bare domain often serves a
    two-line "block everything" file). Mixing them makes a site appear to flip back
    and forth. So each site is analyzed at one canonical host: the one most of its
    post-dense_from captures came from. After dense_from the scheme must match too;
    before it, either scheme is accepted, since most sites moved from http to https
    during 2015-2018.
    """
    manifest = data / "manifest.csv"
    if not manifest.exists():
        sys.exit(f"{manifest} not found. Run fetch.py first.")
    raw: dict[str, list[dict]] = defaultdict(list)
    with open(manifest, newline="") as f:
        for row in csv.DictReader(f):
            if row["status"] != "kept" or row["domain"] in exclude:
                continue
            p = data / "raw" / row["domain"] / f"{row['timestamp']}.txt"
            if not p.exists():
                continue
            row["text"] = p.read_text(encoding="utf-8", errors="replace")
            raw[row["domain"]].append(row)

    by_site: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    for domain, rows in raw.items():
        recent = Counter(_scheme_host(r["original_url"]) for r in rows if r["timestamp"] >= dense_from)
        if not recent:
            recent = Counter(_scheme_host(r["original_url"]) for r in rows)
        (scheme, host), _ = recent.most_common(1)[0]
        kept = []
        for r in rows:
            s, h = _scheme_host(r["original_url"])
            if h == host and (r["timestamp"] < dense_from or s == scheme):
                kept.append(r)
        kept.sort(key=lambda r: r["timestamp"])
        by_site[domain] = kept
        meta[domain] = {"canonical_url": f"{scheme}://{host}/robots.txt",
                        "dropped_other_urls": len(rows) - len(kept)}
    return by_site, meta


def evaluate(text: str, bots: list[dict], path: str) -> dict:
    groups = parse(text)
    status = {}
    for b in bots:
        t = b["token"]
        status[t] = {
            "blocked": not is_allowed(groups, t, path),
            "named": is_named(groups, t),
        }
    star_blocks = not is_allowed(groups, "zz-unlisted-crawler", path)
    return {"bots": status, "star_blocks": star_blocks}


def companies_with_split(bots: list[dict]) -> dict[str, dict[str, list[str]]]:
    by_co: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"training": [], "search": []})
    for b in bots:
        if b["role"] in ("training", "search"):
            by_co[b["company"]][b["role"]].append(b["token"])
    return {co: roles for co, roles in by_co.items() if roles["training"] and roles["search"]}


def splits(ev: dict, pairs: dict, explicit: bool) -> list[str]:
    """Companies whose training crawler is blocked while their search crawler is allowed.

    explicit=True additionally requires the search crawler to be named in the file,
    i.e. the site wrote a rule for it rather than letting it fall through to "*".
    """
    out = []
    for co, roles in pairs.items():
        training_blocked = any(ev["bots"][t]["blocked"] for t in roles["training"])
        search_ok = all(not ev["bots"][t]["blocked"] for t in roles["search"])
        named = any(ev["bots"][t]["named"] for t in roles["search"])
        if training_blocked and search_ok and (named or not explicit):
            out.append(co)
    return out


def half_year(date: str) -> str:
    return f"{date[:4]}-H{1 if int(date[5:7]) <= 6 else 2}"


def normalized_lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]


CHATGPT_RELEASE = "2022-11-30"


def signature(ev: dict, ai_tokens: list[str]) -> tuple:
    return tuple((ev["bots"][t]["blocked"], ev["bots"][t]["named"]) for t in ai_tokens) + (ev["star_blocks"],)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data")
    ap.add_argument("--bots", default="bots.csv")
    ap.add_argument("--out", default="results")
    ap.add_argument("--path", default=DEFAULT_PATH, help=f"content path to test (default {DEFAULT_PATH})")
    ap.add_argument("--dense-from", default="2022-07-01", help="must match the value used in fetch.py")
    ap.add_argument("--exclude", default="", help="comma-separated domains to leave out")
    args = ap.parse_args()

    bots = load_bots(Path(args.bots))
    ai_tokens = [b["token"] for b in bots if b["ai"]]
    pairs = companies_with_split(bots)
    exclude = {d.strip() for d in args.exclude.split(",") if d.strip()}
    by_site, meta = load_snapshots(Path(args.data), args.dense_from.replace("-", ""), exclude)
    by_site = {d: s for d, s in by_site.items() if s}
    if not by_site:
        sys.exit("No kept snapshots found in the manifest.")

    out = Path(args.out)
    (out / "diffs").mkdir(parents=True, exist_ok=True)

    snapshot_rows, change_rows, summary_rows = [], [], []
    timeline: Counter = Counter()
    categories = set()

    for domain, snaps in sorted(by_site.items()):
        category = snaps[0]["category"]
        categories.add(category)
        evals = [evaluate(s["text"], bots, args.path) for s in snaps]
        sigs = [signature(ev, ai_tokens) for ev in evals]

        # --- per-snapshot table
        for s, ev in zip(snaps, evals):
            row = {"domain": domain, "category": category, "date": s["date"],
                   "wayback_url": s["wayback_url"], "star_blocks_all": int(ev["star_blocks"])}
            for b in bots:
                t = b["token"]
                row[t] = "blocked" if ev["bots"][t]["blocked"] else "allowed"
                row[f"{t}_named"] = int(ev["bots"][t]["named"])
            snapshot_rows.append(row)

        # --- transitions and diffs
        diff_chunks = []
        sustained_reversals, flip_flops = [], 0
        for i in range(1, len(snaps)):
            a, b_ = snaps[i - 1], snaps[i]
            ea, eb = evals[i - 1], evals[i]
            # A change "reverts" if the very next version goes straight back to the old state.
            reverted = i + 1 < len(snaps) and sigs[i + 1] == sigs[i - 1] and sigs[i] != sigs[i - 1]
            if reverted:
                flip_flops += 1
            changes = []
            for t in ai_tokens:
                sa, sb = ea["bots"][t], eb["bots"][t]
                if sa["blocked"] != sb["blocked"]:
                    changes.append(f"{t}: {'blocked' if sa['blocked'] else 'allowed'}"
                                   f"→{'blocked' if sb['blocked'] else 'allowed'}")
                    if sa["blocked"] and not sb["blocked"] and not reverted and b_["date"] >= CHATGPT_RELEASE:
                        sustained_reversals.append(f"{t} unblocked {b_['date']}")
                elif sa["named"] != sb["named"]:
                    changes.append(f"{t}: {'now named' if sb['named'] else 'no longer named'}"
                                   f" ({'blocked' if sb['blocked'] else 'allowed'})")
            if ea["star_blocks"] != eb["star_blocks"]:
                changes.append("* group: " + ("now blocks all unlisted crawlers" if eb["star_blocks"]
                                              else "no longer blocks all"))

            la, lb = normalized_lines(a["text"]), normalized_lines(b_["text"])
            diff = list(difflib.unified_diff(la, lb, lineterm="", n=2))
            if not diff:
                continue
            added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            tag = ("[AI, reverted next] " if reverted else "[AI] ") if changes else ""
            diff_chunks.append(
                f"{'=' * 78}\n{tag}{a['date']}  →  {b_['date']}   (+{added} / -{removed} lines)\n"
                f"  before: {a['wayback_url']}\n  after:  {b_['wayback_url']}\n"
                + ("  AI changes: " + "; ".join(changes) + "\n" if changes else "")
                + "\n".join(diff[2:]) + "\n"
            )
            if changes:
                change_rows.append({
                    "domain": domain, "category": category,
                    "from_date": a["date"], "to_date": b_["date"],
                    "changes": "; ".join(changes),
                    "reverted_at_next_version": "yes" if reverted else "no",
                    "lines_added": added, "lines_removed": removed,
                    "before_url": a["wayback_url"], "after_url": b_["wayback_url"],
                })

        header = (f"robots.txt history for {domain} ({category})\n"
                  f"Analyzed URL: {meta[domain]['canonical_url']}\n"
                  f"{len(snaps)} versions, {snaps[0]['date']} to {snaps[-1]['date']}\n"
                  f"Changes marked [AI] touch at least one tracked AI crawler; "
                  f"'reverted next' means the following version went straight back.\n\n")
        (out / "diffs" / f"{domain}.txt").write_text(header + "\n".join(diff_chunks), encoding="utf-8")

        # --- site summary
        def named_blocked(ev):
            return {t for t in ai_tokens if ev["bots"][t]["named"] and ev["bots"][t]["blocked"]}

        pre = [(s, ev) for s, ev in zip(snaps, evals) if s["date"] < CHATGPT_RELEASE]
        post = [(s, ev) for s, ev in zip(snaps, evals) if s["date"] >= CHATGPT_RELEASE]
        baseline = named_blocked(pre[-1][1]) if pre else set()
        first_pre_block = next(((s["date"], named_blocked(ev)) for s, ev in pre if named_blocked(ev)), None)

        first_mention = first_era_block = first_split = first_deny = None
        for s, ev in zip(snaps, evals):
            named = [t for t in ai_tokens if ev["bots"][t]["named"]]
            if named and first_mention is None:
                first_mention = (s["date"], named)
            if ev["star_blocks"] and first_deny is None:
                first_deny = s["date"]
        for s, ev in post:
            new = named_blocked(ev) - baseline
            if new and first_era_block is None:
                first_era_block = (s["date"], [t for t in ai_tokens if t in new])
            sp = splits(ev, pairs, explicit=True)
            if sp and first_split is None:
                first_split = (s["date"], sp)

        # Alternation: how often the file returns to the state it had two changes earlier
        # (A -> B -> A, however long each lasted). High values suggest one URL serving
        # different files to different visitors rather than deliberate edits.
        collapsed = []
        for s, sig in zip(snaps, sigs):
            if s["date"] >= CHATGPT_RELEASE and (not collapsed or collapsed[-1] != sig):
                collapsed.append(sig)
        returns = sum(1 for i in range(2, len(collapsed)) if collapsed[i] == collapsed[i - 2])

        last, ev_last = snaps[-1], evals[-1]
        blocked_now = [t for t in ai_tokens if ev_last["bots"][t]["blocked"]]
        allowed_named_now = [b["token"] for b in bots
                             if ev_last["bots"][b["token"]]["named"] and not ev_last["bots"][b["token"]]["blocked"]]
        if first_era_block:
            timeline[(half_year(first_era_block[0]), category)] += 1

        summary_rows.append({
            "domain": domain,
            "category": category,
            "analyzed_url": meta[domain]["canonical_url"],
            "versions": len(snaps),
            "dropped_other_urls": meta[domain]["dropped_other_urls"],
            "first_version": snaps[0]["date"],
            "latest_version": last["date"],
            "pre_chatgpt_block": first_pre_block[0] if first_pre_block else "",
            "pre_chatgpt_block_bots": " ".join(t for t in ai_tokens if first_pre_block and t in first_pre_block[1]),
            "first_ai_mention": first_mention[0] if first_mention else "",
            "first_ai_mention_bots": " ".join(first_mention[1]) if first_mention else "",
            "first_ai_era_block": first_era_block[0] if first_era_block else "",
            "first_ai_era_block_bots": " ".join(first_era_block[1]) if first_era_block else "",
            "ai_blocked_now": " ".join(blocked_now),
            "default_deny_now": int(ev_last["star_blocks"]),
            "default_deny_first_seen": first_deny or "",
            "named_and_allowed_now": " ".join(allowed_named_now),
            "split_now_explicit": " ".join(splits(ev_last, pairs, explicit=True)),
            "split_now_effective": " ".join(splits(ev_last, pairs, explicit=False)),
            "first_explicit_split": first_split[0] if first_split else "",
            "first_explicit_split_companies": " ".join(first_split[1]) if first_split else "",
            "sustained_reversals": "; ".join(sustained_reversals),
            "flip_flops": flip_flops,
            "returns_to_earlier_state": returns,
            "latest_wayback_url": last["wayback_url"],
        })

    # --- write tables
    write_csv(out / "site_summary.csv", summary_rows)
    write_csv(out / "ai_changes.csv", change_rows)
    write_csv(out / "snapshots.csv", snapshot_rows)
    cats = sorted(categories)
    periods = sorted({p for p, _ in timeline})
    write_csv(out / "first_block_timeline.csv",
              [{"half_year": p, **{c: timeline[(p, c)] for c in cats}, "total": sum(timeline[(p, c)] for c in cats)}
               for p in periods])

    # --- console summary
    n = len(summary_rows)
    era = [r for r in summary_rows if r["first_ai_era_block"]]
    pre = [r for r in summary_rows if r["pre_chatgpt_block"]]
    print(f"Sites analyzed: {n}   versions: {len(snapshot_rows)}   test path: {args.path}")
    print(f"Sites that blocked a now-AI crawler before ChatGPT ({CHATGPT_RELEASE}): {len(pre)}/{n}")
    print(f"Sites that newly blocked an AI crawler after ChatGPT: {len(era)}/{n}")
    if era:
        dates = sorted(r["first_ai_era_block"] for r in era)
        print(f"  earliest: {dates[0]}   latest: {dates[-1]}   median: {dates[len(dates) // 2]}")
    print(f"Sites with an explicit training/search split now: "
          f"{sum(1 for r in summary_rows if r['split_now_explicit'])}/{n}")
    print(f"Sites whose '*' group now blocks every unlisted crawler: "
          f"{sum(r['default_deny_now'] for r in summary_rows)}/{n}")
    print(f"Sites with a sustained reversal: {sum(1 for r in summary_rows if r['sustained_reversals'])}/{n}")
    print(f"Sites with flip-flops (a change undone at the very next version): "
          f"{sum(1 for r in summary_rows if r['flip_flops'])}/{n}")
    alt = [r["domain"] for r in summary_rows if r["returns_to_earlier_state"] >= 3]
    if alt:
        print(f"Sites that keep returning to an earlier state (check for one URL serving two files): {', '.join(alt)}")
    print(f"AI-relevant changes: {len(change_rows)} "
          f"({sum(1 for r in change_rows if r['reverted_at_next_version'] == 'yes')} reverted at the next version)")
    print(f"\nTables written to {out}/ ; per-site diffs in {out}/diffs/")


def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        if not rows:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
