"""
One-shot migration: scan every existing mem0 entry, auto-classify scope + tag,
patch metadata. Safe to re-run (skips entries that already have scope set).

Usage:
    python migrate_scope_tag.py         # dry-run by default, shows plan
    python migrate_scope_tag.py --apply # actually write metadata patches
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load env BEFORE importing modules that read os.environ at import time
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

import nexus_brain  # noqa: E402
import nexus_classifier  # noqa: E402


def _text_of(mem: dict) -> str:
    return mem.get("memory") or mem.get("text") or ""


def _metadata_of(mem: dict) -> dict:
    return dict(mem.get("metadata") or {})


def _id_of(mem: dict) -> str:
    return str(mem.get("id") or mem.get("memory_id") or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (omit for dry-run)")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between classifier calls")
    args = ap.parse_args()

    m = nexus_brain._get_mem0()
    print("[migrate] pulling all memories…")
    try:
        raw = m.get_all()
    except Exception as e:
        print(f"[migrate] get_all() failed: {type(e).__name__}: {e}")
        sys.exit(1)

    mems = raw.get("results", []) if isinstance(raw, dict) else raw
    print(f"[migrate] found {len(mems or [])} memories")

    planned, skipped = [], 0
    for mem in mems or []:
        md = _metadata_of(mem)
        if md.get("scope") in ("personal", "tnc", "public"):
            skipped += 1
            continue
        text = _text_of(mem)
        if not text.strip():
            skipped += 1
            continue
        mid = _id_of(mem)
        if not mid:
            skipped += 1
            continue
        planned.append({"id": mid, "text": text, "metadata": md})

    print(f"[migrate] planned patches: {len(planned)}  (already-scoped skipped: {skipped})")

    if not args.apply:
        for i, p in enumerate(planned[:8]):
            print(f"  sample {i+1}: `{p['id'][:12]}` — {p['text'][:80]}")
        if len(planned) > 8:
            print(f"  …plus {len(planned)-8} more")
        print("\n[migrate] dry-run only. re-run with --apply to commit.")
        return

    # Apply
    patched, failed = 0, 0
    for i, p in enumerate(planned, 1):
        cls = nexus_classifier.classify(p["text"])
        new_md = dict(p["metadata"])
        new_md["scope"] = cls["scope"]
        new_md["tag"] = cls["tag"]
        try:
            m.update(memory_id=p["id"], data=p["text"], metadata=new_md)
            patched += 1
            if i % 10 == 0 or i == len(planned):
                print(f"[migrate] patched {i}/{len(planned)}")
        except Exception as e:
            failed += 1
            print(f"[migrate] fail on `{p['id'][:12]}`: {type(e).__name__}: {e}")
        time.sleep(args.sleep)  # don't hammer the classifier API

    print(f"\n[migrate] done. patched={patched} failed={failed}")


if __name__ == "__main__":
    main()
