#!/usr/bin/env python3
"""
GSD-to-Superpowers Bridge (v1 backward-compatibility wrapper)
=============================================================

This script now delegates to gsd_bridge v2. It translates the old v1 CLI
interface to the new subcommand interface for backward compatibility.

v1 usage (still works):
    python gsd_to_superpowers.py .planning/ --pending
    python gsd_to_superpowers.py .planning/ --pending --state -o docs/plans/
    python gsd_to_superpowers.py .planning/phases/02-homepage/02-01-PLAN.md --dry-run

v2 usage (recommended):
    python -m gsd_bridge export .planning/ --pending
    python -m gsd_bridge reconcile docs/plans/
    python -m gsd_bridge status docs/plans/
    python -m gsd_bridge validate .planning/phases/02-homepage/02-01-PLAN.md
"""

import sys
from pathlib import Path


def main():
    # Translate v1 args to v2 export command
    args = sys.argv[1:]

    if not args:
        print(
            "Usage: python gsd_to_superpowers.py <path> [--pending] [--state] "
            "[-o dir] [--dry-run]\n\n"
            "NOTE: This is the v1 wrapper. For v2 features, use:\n"
            "  python -m gsd_bridge export <path> [--pending] [-o dir] [--dry-run]\n"
            "  python -m gsd_bridge reconcile docs/plans/\n"
            "  python -m gsd_bridge status docs/plans/\n"
            "  python -m gsd_bridge validate <plan-file>",
            file=sys.stderr,
        )
        sys.exit(1)

    # The --state flag from v1 isn't directly supported in v2 export
    # (reconcile replaces it), so we strip it and note the change
    has_state = "--state" in args
    if has_state:
        args.remove("--state")

    # Prepend "export" subcommand for v2
    v2_args = ["export"] + args

    print(
        "NOTE: gsd_to_superpowers.py is the v1 wrapper. "
        "Delegating to gsd_bridge v2...",
        file=sys.stderr,
    )

    from gsd_bridge.cli import main as bridge_main
    exit_code = bridge_main(v2_args)

    if has_state:
        print(
            "\nNOTE: --state flag is deprecated in v2. "
            "Use `python -m gsd_bridge reconcile docs/plans/` instead "
            "to generate STATUS.md after execution.",
            file=sys.stderr,
        )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
