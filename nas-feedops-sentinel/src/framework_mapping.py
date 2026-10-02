"""
The framework mapping (policies/framework-mapping.yaml): Sentinel's controls against NIST AI RMF 1.0 and
ISO/IEC 42001:2023.

    python -m src.framework_mapping               # coverage summary
    python -m src.framework_mapping --markdown    # the document, as docs/framework-mapping.md holds it

The mapping is data, validated here. A reference must be a subcategory or Annex A control that exists, a row marked
implemented or partial must cite evidence, and a row that stops short must say where. tests/test_framework_mapping.py
holds the data against the repository and holds docs/framework-mapping.md against what this module generates, so the
document cannot drift from the data.

It is a mapping, not a claim of conformance: ISO/IEC 42001 certifies an organization's management system.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAPPING_PATH = Path(__file__).resolve().parent.parent / "policies" / "framework-mapping.yaml"
DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "framework-mapping.md"

Fit = Literal["direct", "related"]
Status = Literal["implemented", "partial", "not_covered", "organizational", "not_applicable"]
STATUS_ORDER: tuple[str, ...] = ("implemented", "partial", "not_covered", "organizational", "not_applicable")
STATUS_LABEL = {"implemented": "Implemented", "partial": "Partial", "not_covered": "Not covered",
                "organizational": "Organizational", "not_applicable": "Not applicable"}

# NIST AI RMF 1.0 Core: how many subcategories each category has. Checked against NIST AI 100-1's Core tables.
NIST_SUBCATEGORIES: dict[str, dict[int, int]] = {
    "GOVERN": {1: 7, 2: 3, 3: 2, 4: 3, 5: 2, 6: 2},
    "MAP": {1: 6, 2: 3, 3: 5, 4: 2, 5: 2},
    "MEASURE": {1: 3, 2: 13, 3: 3, 4: 3},
    "MANAGE": {1: 4, 2: 4, 3: 2, 4: 3},
}

# ISO/IEC 42001:2023 Annex A: the 38 controls. Numbers and titles checked against a public listing (the standard is
# paywalled); check against a licensed copy before quoting.
ISO_ANNEX_A: dict[str, str] = {
    "A.2.2": "AI policy", "A.2.3": "Alignment with other organizational policies", "A.2.4": "Review of the AI policy",
    "A.3.2": "AI roles and responsibilities", "A.3.3": "Reporting of concerns",
    "A.4.2": "Resource documentation", "A.4.3": "Data resources", "A.4.4": "Tooling resources",
    "A.4.5": "System and computing resources", "A.4.6": "Human resources",
    "A.5.2": "AI system impact assessment process", "A.5.3": "Documentation of AI system impact assessments",
    "A.5.4": "Assessing impacts on individuals or groups", "A.5.5": "Assessing societal impacts of AI systems",
    "A.6.1.2": "Objectives for responsible development of AI systems",
    "A.6.1.3": "Processes for responsible design and development",
    "A.6.2.2": "AI system requirements and specification",
    "A.6.2.3": "Documentation of AI system design and development",
    "A.6.2.4": "AI system verification and validation", "A.6.2.5": "AI system deployment",
    "A.6.2.6": "AI system operation and monitoring", "A.6.2.7": "AI system technical documentation",
    "A.6.2.8": "AI system recording of event logs",
    "A.7.2": "Data for development and enhancement of AI systems", "A.7.3": "Acquisition of data",
    "A.7.4": "Quality of data for AI systems", "A.7.5": "Data provenance", "A.7.6": "Data preparation",
    "A.8.2": "System documentation and information for users", "A.8.3": "External reporting",
    "A.8.4": "Communication of incidents", "A.8.5": "Information for interested parties",
    "A.9.2": "Processes for responsible use of AI systems", "A.9.3": "Objectives for responsible use of AI systems",
    "A.9.4": "Intended use of the AI system",
    "A.10.2": "Allocation of responsibilities", "A.10.3": "Suppliers", "A.10.4": "Customers",
}
# Management-system clauses are cited at clause level only: sub-clause numbering was not verified.
ISO_CLAUSES: dict[int, str] = {4: "Context of the organization", 5: "Leadership", 6: "Planning", 7: "Support",
                               8: "Operation", 9: "Performance evaluation", 10: "Improvement"}

_NIST_REF = re.compile(r"^(GOVERN|MAP|MEASURE|MANAGE) (\d)\.(\d{1,2})$")
_CLAUSE_REF = re.compile(r"^clause (\d{1,2})$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NistRef(_Strict):
    ref: str
    fit: Fit

    @field_validator("ref")
    @classmethod
    def _exists(cls, ref: str) -> str:
        match = _NIST_REF.match(ref)
        if not match:
            raise ValueError(f"{ref!r} is not a NIST AI RMF subcategory like 'GOVERN 1.6'")
        function, category, number = match.group(1), int(match.group(2)), int(match.group(3))
        if not 1 <= number <= NIST_SUBCATEGORIES[function].get(category, 0):
            raise ValueError(f"{ref!r} is not a subcategory in the AI RMF 1.0 Core")
        return ref


class IsoRef(_Strict):
    ref: str
    fit: Fit

    @field_validator("ref")
    @classmethod
    def _exists(cls, ref: str) -> str:
        clause = _CLAUSE_REF.match(ref)
        if clause:
            if int(clause.group(1)) not in ISO_CLAUSES:
                raise ValueError(f"{ref!r} is not a management-system clause (4 to 10)")
        elif ref not in ISO_ANNEX_A:
            raise ValueError(f"{ref!r} is not an ISO/IEC 42001 Annex A control, or a 'clause N' reference")
        return ref


class Source(_Strict):
    name: str
    verified: str


class Control(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    title: str
    what: str
    nist: list[NistRef]
    iso: list[IsoRef]
    status: Status
    note: str | None = None
    evidence: list[str]

    @field_validator("title", "what")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _honest(self) -> "Control":
        if not self.nist and not self.iso:
            raise ValueError(f"{self.id}: maps to nothing")
        if self.status in ("implemented", "partial") and not self.evidence:
            raise ValueError(f"{self.id}: {self.status} with no evidence")
        if self.status == "not_covered" and self.evidence:
            raise ValueError(f"{self.id}: not_covered cites evidence; if something addresses it, it is partial")
        if self.status != "implemented" and not (self.note or "").strip():
            raise ValueError(f"{self.id}: {self.status} needs a note saying where it stops, or why")
        for refs in (self.nist, self.iso):
            seen = [r.ref for r in refs]
            if len(seen) != len(set(seen)):
                raise ValueError(f"{self.id}: a reference is listed twice")
        return self


class Mapping(_Strict):
    version: str
    name: str
    reviewed: date
    description: str
    sources: list[Source]
    controls: list[Control] = Field(min_length=1)

    @field_validator("controls")
    @classmethod
    def _unique_ids(cls, controls: list[Control]) -> list[Control]:
        ids = [c.id for c in controls]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"control ids used twice: {duplicated}")
        return controls


def load_mapping(path: Path | None = None) -> Mapping:
    """Read and validate the mapping. A reference that does not exist, or a row that overstates, is an error."""
    return Mapping.model_validate(yaml.safe_load(Path(path or MAPPING_PATH).read_text(encoding="utf-8")))


# ── Coverage ─────────────────────────────────────────────────────────────────────────────────────────

def nist_ids() -> list[str]:
    return [f"{fn} {cat}.{n}" for fn, cats in NIST_SUBCATEGORIES.items() for cat, count in cats.items()
            for n in range(1, count + 1)]


def status_counts(mapping: Mapping) -> dict[str, int]:
    counted = Counter(c.status for c in mapping.controls)
    return {s: counted.get(s, 0) for s in STATUS_ORDER}


def nist_coverage(mapping: Mapping) -> dict[str, dict[str, list[str]]]:
    """Per AI RMF function: the subcategories some control maps to, directly or not, and those none does."""
    referenced = {r.ref for c in mapping.controls for r in c.nist}
    direct = {r.ref for c in mapping.controls for r in c.nist if r.fit == "direct"}
    out: dict[str, dict[str, list[str]]] = {}
    for function in NIST_SUBCATEGORIES:
        ids = [i for i in nist_ids() if i.startswith(function + " ")]
        out[function] = {"all": ids, "direct": [i for i in ids if i in direct],
                         "referenced": [i for i in ids if i in referenced],
                         "unmapped": [i for i in ids if i not in referenced]}
    return out


def iso_unmapped(mapping: Mapping) -> list[str]:
    referenced = {r.ref for c in mapping.controls for r in c.iso}
    return [ref for ref in ISO_ANNEX_A if ref not in referenced]


# ── The document ─────────────────────────────────────────────────────────────────────────────────────

def _refs(refs: list, titles: dict[str, str] | None = None) -> str:
    """Direct references plain, related ones in italics."""
    shown = [r.ref if r.fit == "direct" else f"*{r.ref}*" for r in refs]
    return ", ".join(shown) if shown else "none"


def render_markdown(mapping: Mapping) -> str:
    coverage = nist_coverage(mapping)
    counts = status_counts(mapping)
    total_nist = sum(len(c["all"]) for c in coverage.values())
    ref_nist = sum(len(c["referenced"]) for c in coverage.values())
    ref_iso = len(ISO_ANNEX_A) - len(iso_unmapped(mapping))
    out: list[str] = [
        "# Framework mapping: NIST AI RMF 1.0 and ISO/IEC 42001:2023",
        "",
        "<!-- Generated from policies/framework-mapping.yaml by `python -m src.framework_mapping --markdown`. "
        "Edit the YAML, not this file: a test fails when they differ. -->",
        "",
        mapping.description.strip(),
        "",
        f"Reviewed {mapping.reviewed.isoformat()}. Version {mapping.version}.",
        "",
        "## How to read it",
        "",
        "- **Direct** references are plain; *related* ones are in italics. Direct means the control does what the "
        "subcategory or Annex A control asks, within its scope. Related means it supports it or covers part of it.",
        "- **Status** says how far the control goes: Implemented (it exists, is enforced or measured here, and has "
        "evidence), Partial (it stops short, and the note says where), Not covered (nothing here addresses it), "
        "Organizational (an organization has to supply it) or Not applicable (with the reason).",
        "- A mapped control is not a conformant system. Every cited file exists, and a test holds that, but whether "
        "a control is *enough* is a judgement for an assessor.",
        "",
        "## What was checked, and against what",
        "",
    ]
    out += [f"- **{s.name}.** {' '.join(s.verified.split())}" for s in mapping.sources]
    out += ["", "## Summary", "", "| Status | Controls |", "|---|---|"]
    out += [f"| {STATUS_LABEL[s]} | {counts[s]} |" for s in STATUS_ORDER]
    out += [f"| **Total** | **{len(mapping.controls)}** |", "",
            f"NIST AI RMF: {ref_nist} of {total_nist} subcategories are referenced by at least one control. "
            f"ISO/IEC 42001 Annex A: {ref_iso} of {len(ISO_ANNEX_A)} controls are referenced.", "",
            "| AI RMF function | Subcategories | Referenced | Directly |", "|---|---|---|---|"]
    out += [f"| {fn.title()} | {len(c['all'])} | {len(c['referenced'])} | {len(c['direct'])} |"
            for fn, c in coverage.items()]

    out += ["", "## Controls", "", "| Control | NIST AI RMF | ISO/IEC 42001 | Status |", "|---|---|---|---|"]
    for c in mapping.controls:
        out.append(f"| [{c.title}](#{c.id}) | {_refs(c.nist)} | {_refs(c.iso)} | {STATUS_LABEL[c.status]} |")

    for c in mapping.controls:
        out += ["", f'<a id="{c.id}"></a>', f"### {c.title}", "", f"**Status:** {STATUS_LABEL[c.status]}", "",
                " ".join(c.what.split())]
        if c.nist:
            out += ["", "**NIST AI RMF:** " + _refs(c.nist)]
        if c.iso:
            out += ["", "**ISO/IEC 42001:** " + ", ".join(
                (f"{r.ref} {ISO_ANNEX_A[r.ref]}" if r.ref in ISO_ANNEX_A
                 else f"{r.ref} ({ISO_CLAUSES[int(r.ref.split()[1])]})") if r.fit == "direct"
                else "*" + (f"{r.ref} {ISO_ANNEX_A[r.ref]}" if r.ref in ISO_ANNEX_A
                            else f"{r.ref} ({ISO_CLAUSES[int(r.ref.split()[1])]})") + "*" for r in c.iso)]
        if c.note:
            out += ["", "**Note:** " + " ".join(c.note.split())]
        if c.evidence:
            out += ["", "**Evidence:**", ""] + [f"- `{path}`" for path in c.evidence]

    out += ["", "## Not referenced by any control", "",
            "These are not necessarily gaps in the system: many are organizational (workforce, culture, engagement "
            "with affected communities) and say nothing about a code repository. They are listed so a reader can "
            "see what the mapping does not touch.", ""]
    for fn, c in coverage.items():
        out.append(f"- **{fn.title()}:** {', '.join(c['unmapped']) if c['unmapped'] else 'none'}")
    out.append(f"- **ISO/IEC 42001 Annex A:** {', '.join(iso_unmapped(mapping)) or 'none'}")
    out += ["", "To regenerate this file: `python -m src.framework_mapping --markdown > docs/framework-mapping.md`.", ""]
    return "\n".join(out)


def render_summary(mapping: Mapping) -> str:
    counts = status_counts(mapping)
    coverage = nist_coverage(mapping)
    lines = [f"{mapping.name} v{mapping.version}, reviewed {mapping.reviewed}: {len(mapping.controls)} controls", ""]
    lines += [f"  {STATUS_LABEL[s]:<16}{counts[s]}" for s in STATUS_ORDER]
    lines += ["", "NIST AI RMF subcategories referenced / directly / total:"]
    lines += [f"  {fn.title():<9}{len(c['referenced']):>3} / {len(c['direct']):>2} / {len(c['all']):>2}"
              for fn, c in coverage.items()]
    lines += ["", "Not covered or organizational:"]
    lines += [f"  {c.id:<38}{STATUS_LABEL[c.status]}" for c in mapping.controls
              if c.status in ("not_covered", "organizational")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.framework_mapping", description=__doc__.split("\n\n")[0])
    parser.add_argument("--markdown", action="store_true", help="print the document docs/framework-mapping.md holds")
    args = parser.parse_args(argv)
    try:
        mapping = load_mapping()
    except Exception as exc:  # noqa: BLE001
        print(f"framework mapping: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.markdown:
        sys.stdout.write(render_markdown(mapping))
    else:
        print(render_summary(mapping))
    return 0


if __name__ == "__main__":
    sys.exit(main())
