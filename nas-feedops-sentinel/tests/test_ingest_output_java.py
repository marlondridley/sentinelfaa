"""
The JVM edge: IngestFolderFileOutput must keep the broker from being told a message
was handled when it was not written.

The stock kit acknowledges (JMS AUTO_ACKNOWLEDGE) when onMessage returns, and its
onMessage catches every exception, so a failed write used to be acknowledged. The
custom Output halts the JVM before onMessage can return instead, so the broker
redelivers. These tests drive the kit's own SolaceConsumerV1.onMessage with fake JMS
messages (tests/java/.../OutputHarness.java) and observe the real process.

They need a JDK and the kit's jar (jumpstart/lib/, git-ignored) and skip without
them, so the container test image, which has neither, does not run them. Only
src.raw_message is imported, so they also run without the app's dependencies:

    python -m pytest tests/test_ingest_output_java.py --noconftest -o addopts=
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.raw_message import message_from_envelope

REPO = Path(__file__).resolve().parents[1]
JAR = REPO / "jumpstart" / "lib" / "jumpstart-jar-with-dependencies.jar"
WRITER = REPO / "jumpstart" / "src" / "main" / "java" / "com" / "harris" / "cinnato" / "outputs" / "IngestFolderFileOutput.java"
HARNESS = REPO / "tests" / "java" / "com" / "harris" / "cinnato" / "solace" / "OutputHarness.java"
CAPTURE = REPO / "tests" / "fixtures" / "swim_kit" / "capture_2026-08-27"
COMMITTED = REPO / "tests" / "fixtures" / "swim_raw"

pytestmark = pytest.mark.skipif(
    not (shutil.which("javac") and shutil.which("java") and JAR.exists()),
    reason="needs a JDK and the kit jar in jumpstart/lib/",
)


@pytest.fixture(scope="module")
def classpath(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("classes")
    compiled = subprocess.run(
        ["javac", "-Xlint:all", "-cp", str(JAR), "-d", str(out), str(WRITER), str(HARNESS)],
        capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    return os.pathsep.join([str(JAR), str(out)])


def run(classpath: str, mode: str, out_dir: Path, *extra: Path) -> subprocess.CompletedProcess:
    # The kit's logging writes ./log/messages.log; run from the scratch classes directory so it lands there.
    workdir = classpath.split(os.pathsep)[1]
    return subprocess.run(
        ["java", "-cp", classpath, "com.harris.cinnato.solace.OutputHarness", mode, str(out_dir), *map(str, extra)],
        capture_output=True, text=True, timeout=120, cwd=workdir)


def records(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.swimmsg"))


def test_the_kit_delivers_a_message_and_the_python_side_can_read_the_record(classpath, tmp_path):
    result = run(classpath, "ok", tmp_path)

    assert result.returncode == 0 and "DELIVERED" in result.stdout
    (record,) = records(tmp_path)
    message = message_from_envelope(record.read_text(encoding="utf-8"), record.name)
    assert message.message_id == "TDES:51190821:1874"
    assert message.headers["tracon"] == "PCT" and message.convert_error is None
    assert json.loads(message.body) == {"msg": {"airport": "KBWI", "n": 7}}      # converted from the XML


def test_nothing_is_left_half_written(classpath, tmp_path):
    run(classpath, "ok", tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [p.name for p in records(tmp_path)]


def test_a_burst_writes_one_file_per_message(classpath, tmp_path):
    """The stock output names a file by the millisecond and truncates on open: messages in one millisecond overwrote each other."""
    result = run(classpath, "burst", tmp_path)

    assert result.returncode == 0
    assert len(records(tmp_path)) == 300
    assert len({message_from_envelope(p.read_text(encoding="utf-8")).message_id for p in records(tmp_path)}) == 300


def test_a_payload_that_is_not_xml_is_kept_and_flagged_not_halted_on(classpath, tmp_path):
    """A data problem, not a delivery problem: halting would have the broker redeliver it forever."""
    result = run(classpath, "badconvert", tmp_path)

    assert result.returncode == 0
    (record,) = records(tmp_path)
    message = message_from_envelope(record.read_text(encoding="utf-8"))
    assert message.body == "this is not xml at all" and message.convert_error


def test_a_failed_write_stops_the_jvm_before_the_kit_can_acknowledge(classpath, tmp_path):
    """The point of the class: onMessage never returns, so AUTO_ACKNOWLEDGE never fires and the broker redelivers."""
    out = tmp_path / "out"
    result = run(classpath, "halt", out)

    assert result.returncode == 1
    assert "RETURNED_NORMALLY" not in result.stdout
    assert out.is_file()                              # the harness replaced the directory with a file; nothing was written into it


def test_control_without_the_halt_the_kit_swallows_the_failure_and_returns(classpath, tmp_path):
    """Why the halt is needed: this is what the stock kit does with any Output failure, and it returns, i.e. acknowledges."""
    result = run(classpath, "softhalt", tmp_path / "out")

    assert "HALT_REQUESTED" in result.stdout and "RETURNED_NORMALLY" in result.stdout


def test_the_writer_refuses_to_start_without_a_place_to_write(classpath, tmp_path):
    unwritable = tmp_path / "a-file"
    unwritable.write_text("not a directory", encoding="utf-8")

    result = run(classpath, "ok", unwritable)

    assert result.returncode != 0 and "DELIVERED" not in result.stdout


def _shape(record: Path) -> dict:
    doc = json.loads(record.read_text(encoding="utf-8"))
    doc.pop("received_at")                            # the moment of writing: the one field that cannot match
    return doc


def test_the_committed_fixtures_are_what_the_writer_produces_today(classpath, tmp_path):
    """The Python tests read tests/fixtures/swim_raw/. If the record layout changes, they must change with it."""
    fresh, identity = tmp_path / "fresh", tmp_path / "identity"
    assert run(classpath, "fromcapture", fresh, CAPTURE).returncode == 0
    assert run(classpath, "nogid", identity, CAPTURE).returncode == 0

    for produced, committed in (
        (records(fresh), records(COMMITTED / "capture_2026-08-27")),
        (records(identity), records(COMMITTED / "no_identity")),
    ):
        assert len(produced) == len(committed) > 0
        for now, then in zip(produced, committed):
            assert _shape(now) == _shape(then), then.name
