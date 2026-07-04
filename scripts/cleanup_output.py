#!/usr/bin/env python3
"""
cleanup_output.py — delete MiroFish output files older than the current month.

Keeps:
  - All prediction_*.md and brief_*.md files from the current calendar month
  - .pipeline_state.json (always — needed for resume)

Deletes:
  - prediction_YYYYMMDD_*.md older than current month
  - brief_YYYYMMDD_*.md older than current month
  - Old *.bak / *.backup-* files (any age)

Usage:
  python3 cleanup_output.py             # dry-run (default)
  python3 cleanup_output.py --apply     # actually delete
  python3 cleanup_output.py --verbose   # log every action even in dry-run
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Filename patterns to recognize as dated MiroFish outputs.
PREDICTION_RE = re.compile(r"^prediction_(\d{8})_(\d{6})\.md$")
BRIEF_RE = re.compile(r"^brief_(\d{8})_(\d{6})\.md$")


def file_date(p: Path) -> str:
    """Extract YYYYMMDD from a prediction_/brief_ filename. Empty string if no match."""
    m = PREDICTION_RE.match(p.name) or BRIEF_RE.match(p.name)
    return m.group(1) if m else ""


def should_delete(p: Path, cutoff_yyyymm: str) -> bool:
    """Return True if the file should be deleted (older than current month)."""
    name = p.name
    if name == ".pipeline_state.json":
        return False  # never delete the checkpoint
    # dated outputs
    date = file_date(p)
    if date and date < cutoff_yyyymm:
        return True
    # any .bak / .backup-* regardless of age
    if ".bak" in name or ".backup" in name:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Delete MiroFish output files older than current month")
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry-run)")
    parser.add_argument("--verbose", action="store_true", help="Log every checked file")
    args = parser.parse_args()

    if not OUTPUT_DIR.exists():
        print(f"output dir does not exist: {OUTPUT_DIR}")
        sys.exit(1)

    cutoff = datetime.now().strftime("%Y%m")  # e.g. "202607"
    deleted = 0
    kept = 0
    bytes_freed = 0

    for f in sorted(OUTPUT_DIR.iterdir()):
        if not f.is_file():
            continue
        if should_delete(f, cutoff):
            size = f.stat().st_size
            if args.apply:
                f.unlink()
                action = "deleted"
            else:
                action = "would delete"
            print(f"  {action}: {f.name} ({size:,} bytes)")
            deleted += 1
            bytes_freed += size
        elif args.verbose:
            print(f"  keep:     {f.name}")
            kept += 1

    mode = "applied" if args.apply else "dry-run"
    print(f"\n{mode}: {deleted} file(s) marked for deletion, {bytes_freed:,} bytes")
    print(f"cutoff: anything dated before {cutoff} (year-month)")
    if not args.apply and deleted > 0:
        print("  (pass --apply to actually delete)")


if __name__ == "__main__":
    main()