#!/usr/bin/env python3
"""Keep the Claude Code copy of the project skills identical to the canonical one.

The skills use the Agent Skills format (https://agentskills.io), so most tools can read them, but not from one
folder: Codex and Gemini CLI read .agents/skills/ and not .claude/skills/, while Claude Code reads .claude/skills/
and not .agents/skills/. So:

    .agents/skills/   the canonical copy. Edit here. Codex, Gemini CLI, Copilot, Cursor and OpenCode read it.
    .claude/skills/   a generated, committed copy for Claude Code (Copilot, Cursor and OpenCode read it too).

Real copies, not symlinks: this repository is developed on Windows with core.symlinks=false, where a committed
symlink checks out as a plain text file and the skill silently disappears.

    python scripts/sync_agent_skills.py           copy canonical -> mirror
    python scripts/sync_agent_skills.py --check   exit 1 if the mirror differs (for CI or a pre-commit hook)

Only skill folders that exist in the canonical copy are touched. A skill that exists only under .claude/skills/
(Claude-only on purpose) is reported and left alone. Line endings are ignored when comparing, because a Windows
checkout may hold CRLF while the repository stores LF.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / ".agents" / "skills"
MIRROR = ROOT / ".claude" / "skills"
_SKIP_PARTS = {"__pycache__", ".git"}


def skill_dirs(base: Path) -> list[Path]:
    """Folders directly under base that hold a SKILL.md, the way every client discovers a skill."""
    if not base.is_dir():
        return []
    return sorted(d for d in base.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())


def read_tree(skill: Path) -> dict[str, bytes]:
    """Relative posix path -> content with CRLF normalised to LF, for every file in a skill folder."""
    files: dict[str, bytes] = {}
    for path in sorted(skill.rglob("*")):
        rel = path.relative_to(skill)
        if path.is_file() and not _SKIP_PARTS.intersection(rel.parts):
            files[rel.as_posix()] = path.read_bytes().replace(b"\r\n", b"\n")
    return files


def differences(name: str) -> list[str]:
    """Human-readable differences between the canonical skill and its mirror; empty when identical."""
    source, copy = CANONICAL / name, MIRROR / name
    if not copy.is_dir():
        return [f"{name}: missing from {MIRROR.relative_to(ROOT).as_posix()}/"]
    a, b = read_tree(source), read_tree(copy)
    problems = [f"{name}/{rel}: missing from the mirror" for rel in sorted(a.keys() - b.keys())]
    problems += [f"{name}/{rel}: only in the mirror (delete it or run sync)" for rel in sorted(b.keys() - a.keys())]
    problems += [f"{name}/{rel}: differs from the canonical copy" for rel in sorted(a.keys() & b.keys()) if a[rel] != b[rel]]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report differences and exit 1 instead of copying")
    args = parser.parse_args()

    skills = skill_dirs(CANONICAL)
    if not skills:
        print(f"no skills found under {CANONICAL.relative_to(ROOT).as_posix()}/", file=sys.stderr)
        return 2
    names = [s.name for s in skills]

    claude_only = [d.name for d in skill_dirs(MIRROR) if d.name not in names]
    if claude_only:
        print(f"left alone (only under {MIRROR.relative_to(ROOT).as_posix()}/): {', '.join(claude_only)}")

    if args.check:
        problems = [p for name in names for p in differences(name)]
        for p in problems:
            print(p, file=sys.stderr)
        if problems:
            print("mirror is out of date: python scripts/sync_agent_skills.py", file=sys.stderr)
            return 1
        print(f"ok: {len(names)} skills, mirror identical to canonical")
        return 0

    MIRROR.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        target = MIRROR / skill.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target, ignore=shutil.ignore_patterns(*_SKIP_PARTS))
        print(f"synced {skill.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
