"""
The framework mapping (policies/framework-mapping.yaml) is held to the repository and to its document.

A mapping that drifts is worse than none: it reads as assurance. These tests fail when a reference is not a real
NIST AI RMF subcategory or ISO/IEC 42001 Annex A control, when a cited file does not exist, when a row claims more
than it can show, and when docs/framework-mapping.md no longer matches what the data generates.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src import framework_mapping as fm

APP = Path(__file__).resolve().parent.parent
REPO = APP.parent
MAPPING = fm.load_mapping()


def _raw() -> dict:
    return copy.deepcopy(yaml.safe_load(fm.MAPPING_PATH.read_text(encoding="utf-8")))


# ── The data is held to the repository ───────────────────────────────────────────────────────────────

# What the test image carries of this folder (docker/Dockerfile's COPY lines). A file at the top of the folder, such
# as cve-allowlist.md, is in a checkout and not in the image, so the image cannot be asked whether it exists.
IMAGE_DIRS = ("src", "tests", "docs", "policies", "monitoring", "dashboard", "prompts", "scripts")
FULL_CHECKOUT = (APP / "docker" / "Dockerfile").exists()


def _resolve(path: str) -> Path | None:
    """
    A path from the repository root, or None when this run cannot see it: the test image holds only some of the
    application folder and none of the rest of the repository. A developer's checkout sees all of it.

    CI also runs this test against the whole checkout with REQUIRE_FULL_CHECKOUT=1, where a path it cannot see is
    a failure, not a skip: otherwise the files the image does not carry (the workflows, instraclustr/, the README)
    would never be checked by anything that blocks a merge.
    """
    def unseen() -> None:
        assert not os.environ.get("REQUIRE_FULL_CHECKOUT"), (
            f"{path}: this run cannot see it, and REQUIRE_FULL_CHECKOUT says it must")

    if path.startswith("nas-feedops-sentinel/"):
        inside = path.removeprefix("nas-feedops-sentinel/")
        if not FULL_CHECKOUT and inside.split("/")[0] not in IMAGE_DIRS:
            return unseen()
        return APP / inside
    if not (REPO / "nas-feedops-sentinel").is_dir():
        return unseen()
    return REPO / path


def test_a_run_that_must_see_the_whole_checkout_fails_instead_of_skipping_what_it_cannot_see(monkeypatch, tmp_path):
    monkeypatch.setitem(globals(), "FULL_CHECKOUT", False)                 # what the test image looks like
    monkeypatch.setitem(globals(), "REPO", tmp_path)                       # and it has no repository around it
    monkeypatch.delenv("REQUIRE_FULL_CHECKOUT", raising=False)
    assert _resolve("nas-feedops-sentinel/cve-allowlist.md") is None       # skipped, as in the image
    assert _resolve(".github/workflows/pyapp-ci.yml") is None
    assert _resolve("nas-feedops-sentinel/src/redaction.py") is not None   # in the image: never a problem

    monkeypatch.setenv("REQUIRE_FULL_CHECKOUT", "1")
    with pytest.raises(AssertionError, match="cve-allowlist.md"):
        _resolve("nas-feedops-sentinel/cve-allowlist.md")
    with pytest.raises(AssertionError, match="pyapp-ci.yml"):
        _resolve(".github/workflows/pyapp-ci.yml")
    assert _resolve("nas-feedops-sentinel/src/redaction.py") is not None


def test_every_file_the_mapping_cites_exists():
    missing = []
    for control in MAPPING.controls:
        for path in control.evidence:
            resolved = _resolve(path)
            if resolved is not None and not resolved.exists():
                missing.append(f"{control.id}: {path}")
    assert not missing, missing


def test_evidence_is_cited_once_per_row_and_from_the_repository_root():
    for control in MAPPING.controls:
        assert len(control.evidence) == len(set(control.evidence)), control.id
        assert not any(p.startswith(("/", "../", "./")) for p in control.evidence), control.id


def test_the_agent_register_is_cited_where_the_mapping_leans_on_it():
    cited = {path for c in MAPPING.controls for path in c.evidence}
    assert "nas-feedops-sentinel/policies/agent-register.yaml" in cited


# ── The data says what it covers ─────────────────────────────────────────────────────────────────────

def test_the_reference_tables_are_the_size_of_the_frameworks():
    assert len(fm.nist_ids()) == 72, "the AI RMF 1.0 Core has 72 subcategories"
    assert len(fm.ISO_ANNEX_A) == 38, "ISO/IEC 42001 Annex A has 38 controls"
    assert {k: sum(v.values()) for k, v in fm.NIST_SUBCATEGORIES.items()} == {
        "GOVERN": 19, "MAP": 18, "MEASURE": 22, "MANAGE": 13}


def test_every_function_of_the_ai_rmf_is_touched_and_something_is_directly_covered_in_each():
    coverage = fm.nist_coverage(MAPPING)
    for function, found in coverage.items():
        assert found["referenced"], f"{function} is not referenced by any control"
        assert found["direct"], f"{function} has no directly covered subcategory"


def test_the_mapping_admits_what_it_does_not_cover():
    counts = fm.status_counts(MAPPING)

    assert counts["not_covered"] >= 1 and counts["partial"] >= 1 and counts["organizational"] >= 1
    assert sum(counts.values()) == len(MAPPING.controls)
    assert fm.iso_unmapped(MAPPING), "every Annex A control referenced would be a claim worth checking"


def test_a_row_that_stops_short_says_where():
    for control in MAPPING.controls:
        if control.status != "implemented":
            assert (control.note or "").strip(), control.id
        if control.status in ("implemented", "partial"):
            assert control.evidence, control.id
        if control.status == "not_covered":
            assert not control.evidence, control.id


# ── The document is generated from the data ──────────────────────────────────────────────────────────

def test_the_committed_document_is_what_the_data_generates():
    committed = fm.DOC_PATH.read_text(encoding="utf-8")

    assert committed == fm.render_markdown(MAPPING), (
        "docs/framework-mapping.md is stale: regenerate it with "
        "`python -m src.framework_mapping --markdown > docs/framework-mapping.md`")


def test_the_document_has_a_section_and_an_anchor_for_every_control():
    document = fm.render_markdown(MAPPING)

    for control in MAPPING.controls:
        assert f'<a id="{control.id}"></a>' in document and f"](#{control.id})" in document
        assert f"### {control.title}" in document


def test_the_document_says_it_is_a_mapping_and_what_was_verified():
    document = fm.render_markdown(MAPPING)

    assert "Mapped does not mean conformant" in document
    assert "paywalled" in document and "licensed copy" in document
    assert "Not referenced by any control" in document


# ── A reference must be real, and a row must not overstate ───────────────────────────────────────────

@pytest.mark.parametrize("ref", ["GOVERN 7.1", "GOVERN 1.8", "MAP 6.1", "MAP 3.6", "MEASURE 2.14", "MANAGE 4.4",
                                 "FOO 1.1", "govern 1.1", "MAP 1", "MAP 1.0", "", "GOVERN 1.1\n", " GOVERN 1.1"])
def test_a_nist_reference_that_is_not_a_subcategory_is_refused(ref):
    raw = _raw()
    raw["controls"][0]["nist"] = [{"ref": ref, "fit": "direct"}]

    with pytest.raises(ValidationError, match="NIST|subcategory"):
        fm.Mapping.model_validate(raw)


@pytest.mark.parametrize("ref", ["A.1.2", "A.11.2", "A.6.2.9", "A.5.6", "A.6.2", "clause 3", "clause 11", "6.1.4", "", "clause 4\n"])
def test_an_iso_reference_that_is_not_a_control_or_a_clause_is_refused(ref):
    raw = _raw()
    raw["controls"][0]["iso"] = [{"ref": ref, "fit": "direct"}]

    with pytest.raises(ValidationError, match="Annex A|clause"):
        fm.Mapping.model_validate(raw)


@pytest.mark.parametrize("ref", ["GOVERN 1.1", "GOVERN 1.7", "MEASURE 2.13", "MANAGE 4.3", "MAP 5.2"])
def test_the_first_and_last_real_subcategories_are_accepted(ref):
    assert fm.NistRef(ref=ref, fit="related").ref == ref


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r["controls"][0].update(status="implemented", evidence=[]), "no evidence"),
    (lambda r: r["controls"][0].update(status="partial", evidence=[]), "no evidence"),
    (lambda r: r["controls"][0].update(status="partial", note=" "), "needs a note"),
    (lambda r: r["controls"][0].update(status="organizational", note=None), "needs a note"),
    (lambda r: r["controls"][0].update(status="not_covered", note="x"), "not_covered cites evidence"),
    (lambda r: r["controls"][0].update(status="mostly"), "status"),
    (lambda r: r["controls"][0].update(nist=[], iso=[]), "maps to nothing"),
    (lambda r: r["controls"][0].update(nist=[{"ref": "MAP 1.1", "fit": "direct"}, {"ref": "MAP 1.1", "fit": "related"}]),
     "listed twice"),
    (lambda r: r["controls"][0]["nist"][0].update(fit="exact"), "fit"),
    (lambda r: r["controls"][0].update(title=" "), "blank"),
    (lambda r: r["controls"][0].update(unexpected="x"), "unexpected"),
    (lambda r: r["controls"].append(copy.deepcopy(r["controls"][0])), "used twice"),
    (lambda r: r["controls"][0].update(id="Bad Id"), "id"),
])
def test_a_mapping_that_overstates_or_is_malformed_does_not_load(mutate, message):
    raw = _raw()
    mutate(raw)

    with pytest.raises(ValidationError, match=message):
        fm.Mapping.model_validate(raw)


def test_the_mapping_can_be_read_from_another_path(tmp_path):
    path = tmp_path / "mapping.yaml"
    path.write_text(fm.MAPPING_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    assert fm.load_mapping(path).name == MAPPING.name


# ── The command line ─────────────────────────────────────────────────────────────────────────────────

def test_the_command_prints_a_summary_and_what_is_not_covered(capsys):
    assert fm.main([]) == 0
    out = capsys.readouterr().out

    assert f"{len(MAPPING.controls)} controls" in out and "Not covered" in out
    assert "impact-assessment" in out and "ai-policy-and-management-system" in out


def test_the_command_prints_the_document(capsys):
    assert fm.main(["--markdown"]) == 0
    assert capsys.readouterr().out == fm.render_markdown(MAPPING)


def test_a_mapping_that_cannot_be_read_is_exit_code_two(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fm, "MAPPING_PATH", tmp_path / "missing.yaml")

    assert fm.main([]) == 2
    assert "framework mapping:" in capsys.readouterr().err
