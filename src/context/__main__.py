"""
Minimal consistency maintenance helper.

Usage:
    python -m src.context --repo ~/airforce2                    # report only
    python -m src.context --repo ~/airforce2 --backfill         # backfill SHA with current HEAD (safe)
    python -m src.context --repo ~/airforce2 --repair           # reset to pending (aggressive)
"""

import argparse
import subprocess

from .store import ContextStore


def _get_head_sha(repo_dir: str) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True
    ).strip()


def main():
    parser = argparse.ArgumentParser(description="Task DB consistency helper")
    parser.add_argument("--repo", required=True, help="Target repo path")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--backfill", action="store_true", help="Backfill missing SHA with current HEAD (safe)")
    group.add_argument("--repair", action="store_true", help="Reset unverifiable tasks to pending (aggressive)")
    args = parser.parse_args()

    store = ContextStore(args.repo)
    try:
        report = store.consistency_report()
        print(f"completed:   {report['completed']}")
        print(f"  with SHA:  {report['with_sha']}")
        print(f"  no SHA:    {report['without_sha']}")

        if report["without_sha"] == 0:
            print("\n✅ All completed tasks have merge_commit SHA")
            return

        if not args.backfill and not args.repair:
            print(f"\n⚠️ {report['without_sha']} tasks without SHA")
            print("  --backfill  trust DB, fill SHA with current HEAD (safe)")
            print("  --repair    reset to pending and re-run (aggressive)")
            return

        if args.backfill:
            head = _get_head_sha(args.repo)
            ids = store.backfill_merge_commits(head)
            print(f"\n🔧 Backfilled {len(ids)} tasks with HEAD {head[:8]}:")
            for tid in ids:
                t = store.get_task(tid)
                print(f"  - {tid[:8]}  {t.title if t else '?'}")
        else:
            ids = store.repair_unverifiable()
            print(f"\n⚠️ Reset {len(ids)} tasks to pending:")
            for tid in ids:
                t = store.get_task(tid)
                print(f"  - {tid[:8]}  {t.title if t else '?'}")

        after = store.consistency_report()
        print(f"\n✅ After: {after['completed']} completed, {after['without_sha']} without SHA")
    finally:
        store.close()


if __name__ == "__main__":
    main()
