"""
The SWIM subscription password must never be a command-line argument.

run-live.ps1 promised exactly that in its header and then passed -Dpassword=...
to java, where the process list and command-line process auditing (Windows
event 4688, Sysmon event 1) record it in clear.
"""
from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parent.parent


def _code(path: Path) -> list[str]:
    """Lines that run: comments, the comment-based help block and blank lines are where the rule is explained."""
    text = path.read_text(encoding="utf-8")
    if "<#" in text:
        text = text.split("<#", 1)[0] + text.split("#>", 1)[1]
    return [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_the_live_launcher_does_not_put_the_password_on_the_command_line():
    launcher = _code(APP / "jumpstart" / "run-live.ps1")
    assert not [line for line in launcher if "-Dpassword" in line], "the password is in the java arguments"
    assert any("-Dconfig.file=" in line for line in launcher), "it reaches the kit through a private config file"


def test_the_documented_manual_launch_does_not_either():
    manual = (APP / "docs" / "operations.md").read_text(encoding="utf-8")
    example = manual.split("```bat", 1)[1].split("```", 1)[0]
    assert "-Dpassword" not in example


# ── the launcher runs the output that acknowledges only what it wrote ─────────
#
# The stock kit acknowledges a message when its output returns, and MessageFileOutput returns after
# logging a failed write. See IngestFolderFileOutput and tests/test_ingest_output_java.py.

def test_the_live_launcher_uses_the_output_that_stops_the_kit_when_it_cannot_write():
    launcher = _code(APP / "jumpstart" / "run-live.ps1")
    assert any("-Doutput=com.harris.cinnato.outputs.IngestFolderFileOutput" in line for line in launcher)
    assert not any("MessageFileOutput" in line for line in launcher)
    assert any("-DoutputDir=" in line for line in launcher), "the output refuses to start without a destination"
    assert any("com.harris.cinnato.Consumer" in line and "-cp" in line for line in launcher), (
        "the custom output has to be on the kit's classpath, which -jar would not allow")


def test_the_launcher_needs_both_json_and_headers_because_the_record_carries_them():
    launcher = _code(APP / "jumpstart" / "run-live.ps1")
    for flag in ("-Djson=true", "-Dheaders=true"):
        assert any(flag in line for line in launcher), flag


def test_the_launcher_restarts_the_kit_because_it_now_exits_on_a_failed_write():
    text = "\n".join(_code(APP / "jumpstart" / "run-live.ps1"))
    assert "while ($true)" in text and "Start-Sleep" in text and "$NoRestart" in text


def test_the_launcher_builds_the_output_from_the_tracked_source():
    source = APP / "jumpstart" / "src" / "main" / "java" / "com" / "harris" / "cinnato" / "outputs" / "IngestFolderFileOutput.java"
    assert source.exists()
    text = "\n".join(_code(APP / "jumpstart" / "run-live.ps1"))
    assert "IngestFolderFileOutput.java" in text and "javac" in text
