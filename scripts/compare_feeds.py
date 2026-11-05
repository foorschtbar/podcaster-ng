#!/usr/bin/env python3
"""Compare podcast feeds of two Podcaster instances (old vs. new).

    compare_feeds.py <old-base-url> <new-base-url> <podcast-id> [<podcast-id> ...]

For each podcast the feeds <base>/<id>.xml are fetched and compared by item
count, GUIDs, enclosure URLs, lengths and types. Host names are ignored (the
base URL of each side is stripped), so the comparison also works while the two
instances run under different addresses. Exit status: 0 identical, 1 differences,
2 a feed could not be fetched or parsed. Standard library only.
"""

import argparse
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


def fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "podcaster-compare-feeds/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def relative(url: str, base: str) -> str:
    base = base.rstrip("/")
    return url[len(base):] if url.startswith(base + "/") else url


def parse_items(xml: bytes, base: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        items.append({
            "guid": relative((item.findtext("guid") or "").strip(), base),
            "url": relative(enclosure.get("url", ""), base) if enclosure is not None else "",
            "length": enclosure.get("length", "") if enclosure is not None else "",
            "type": enclosure.get("type", "") if enclosure is not None else "",
        })
    return items


def compare(old: list[dict], new: list[dict]) -> list[str]:
    problems = []
    if len(old) != len(new):
        problems.append(f"item count differs: old {len(old)}, new {len(new)}")
    old_by, new_by = {i["guid"]: i for i in old}, {i["guid"]: i for i in new}
    for guid in sorted(old_by.keys() - new_by.keys()):
        problems.append(f"missing in new: {guid}")
    for guid in sorted(new_by.keys() - old_by.keys()):
        problems.append(f"only in new: {guid}")
    for guid in sorted(old_by.keys() & new_by.keys()):
        for key in ("url", "length", "type"):
            if old_by[guid][key] != new_by[guid][key]:
                problems.append(f"{key} differs for {guid}: old {old_by[guid][key]!r}, new {new_by[guid][key]!r}")
    if not problems and [i["guid"] for i in old] != [i["guid"] for i in new]:
        print("  note: same items, different order (apps sort by pubDate)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old_base_url")
    ap.add_argument("new_base_url")
    ap.add_argument("podcast_ids", nargs="+")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    status = 0
    for pid in args.podcast_ids:
        print(f"{pid}:")
        try:
            old = parse_items(fetch(f"{args.old_base_url.rstrip('/')}/{pid}.xml", args.timeout), args.old_base_url)
            new = parse_items(fetch(f"{args.new_base_url.rstrip('/')}/{pid}.xml", args.timeout), args.new_base_url)
        except (urllib.error.URLError, OSError, ET.ParseError) as exc:
            print(f"  ERROR: {exc}")
            status = max(status, 2)
            continue
        problems = compare(old, new)
        if problems:
            status = max(status, 1)
            print(f"  DIFFERENT ({len(old)} old / {len(new)} new items)")
            for p in problems[:50]:
                print(f"  - {p}")
            if len(problems) > 50:
                print(f"  ... and {len(problems) - 50} more")
        else:
            print(f"  identical: {len(old)} items, GUIDs, enclosure URLs, lengths and types match")
    return status


if __name__ == "__main__":
    sys.exit(main())
