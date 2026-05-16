"""Install td-mcp skills into ~/.claude/skills/.

Copies (not symlinks) so the user can edit installed skills without
risking the repo, and so editing the repo doesn't break a running session
mid-flight. Re-run after pulling new skill content.

Usage:
    python scripts/install_skills.py
    python scripts/install_skills.py --dry-run
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "skills"
TARGET = Path.home() / ".claude" / "skills"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without writing.")
    args = parser.parse_args()

    if not SOURCE.exists():
        print(f"No source skills at {SOURCE}", file=sys.stderr)
        return 1

    TARGET.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []

    for skill_dir in sorted(SOURCE.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            print(f"  skip {skill_dir.name} (no SKILL.md)")
            continue

        dst_dir = TARGET / skill_dir.name
        if args.dry_run:
            print(f"  would install {skill_dir.name} → {dst_dir}")
        else:
            dst_dir.mkdir(exist_ok=True)
            # Copy SKILL.md plus any companion files in the source skill dir
            for src_file in skill_dir.iterdir():
                if src_file.is_file():
                    shutil.copy2(src_file, dst_dir / src_file.name)
            print(f"  installed {skill_dir.name} → {dst_dir}")
        installed.append(skill_dir.name)

    if not installed:
        print("No skills found to install.")
        return 1

    print(f"\n{'Would install' if args.dry_run else 'Installed'} {len(installed)} skill(s).")
    if not args.dry_run:
        print("Restart Claude Code to pick up the new skills.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
