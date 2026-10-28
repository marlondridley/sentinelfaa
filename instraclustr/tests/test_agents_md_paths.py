"""
The "What this is" table in AGENTS.md points at folders that exist.

Agents read that table first to find their way around the repository; a row naming a moved or deleted folder sends
them looking in the wrong place. So every backticked path in the table is checked here: the first cell from the
repository root, and the folders and files the second cell names, from the first cell's folder or else the root.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.skipif(
    not (ROOT / ".github").is_dir() or not (ROOT / "AGENTS.md").is_file(), reason="needs the whole repository mounted"
)

FIRST_CELL_PATHS = ("nas-feedops-sentinel/", "instraclustr/", ".github/", ".agents/skills/", "launch_nas_sentinel.ps1")


def _section(text: str) -> str:
    match = re.search(r"(?ms)^## What this is\s*$(.*?)(?=^## )", text)
    assert match, "AGENTS.md has no '## What this is' section followed by another '## ' heading"
    return match.group(1)


def _rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        line = line.strip()
        # The separator row is only pipes, dashes and colons; the header row names the columns.
        if not line.startswith("|") or re.fullmatch(r"[|:\- ]+", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] != "Path":
            rows.append(cells)
    return rows


def _present(path: Path, name: str) -> bool:
    return path.is_dir() if name.endswith("/") else path.exists()


def _path_like(name: str) -> bool:
    # A command such as `python scripts/sync_agent_skills.py` has a space; anything with a colon is not a path.
    return " " not in name and ":" not in name and (name.endswith("/") or bool(re.search(r"\.[A-Za-z0-9]+$", name)))


def _check(text: str, root: Path) -> tuple[list[str], list[str]]:
    """Return the first-cell paths found and a message for each path in the table that does not exist."""
    found, missing = [], []
    for cells in _rows(_section(text)):
        row = " | ".join(cells)
        first = re.findall(r"`([^`]+)`", cells[0])
        if len(first) != 1:
            missing.append(f"row without exactly one backticked path in its first cell: {row}")
            continue
        folder = first[0]
        found.append(folder)
        if not _present(root / folder, folder):
            missing.append(f"{folder} (row: {row})")
        base = root / folder if folder.endswith("/") else root
        for name in re.findall(r"`([^`]+)`", " ".join(cells[1:])):
            if _path_like(name) and not (_present(base / name, name) or _present(root / name, name)):
                missing.append(f"{name} (row: {row})")
    return found, missing


def test_every_path_in_the_what_this_is_table_exists():
    found, missing = _check((ROOT / "AGENTS.md").read_text(encoding="utf-8"), ROOT)
    assert set(FIRST_CELL_PATHS) <= set(found), f"the table lost a row: found only {found}"
    assert not missing, "AGENTS.md 'What this is' names paths that do not exist:\n" + "\n".join(missing)


def test_the_check_reports_a_path_that_does_not_exist(tmp_path):
    (tmp_path / "app" / "src").mkdir(parents=True)
    table = (
        "## What this is\n\n| Path | What is there |\n|---|---|\n"
        "| `app/` | The application. `src/`, `gone/`, `compose.yml`; run `python scripts/x.py` |\n"
        "| `no-such-folder/` | Nothing |\n\n## Next\n"
    )
    found, missing = _check(table, tmp_path)
    assert found == ["app/", "no-such-folder/"]
    assert [m.split(" ")[0] for m in missing] == ["gone/", "compose.yml", "no-such-folder/"]
    assert all("(row: " in m for m in missing)
