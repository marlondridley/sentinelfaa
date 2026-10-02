"""
The Agent Evaluation page gives a person something to do with each number
(dashboard/pages/6_Agent_Evaluation.py).

Its Health table used to read "Evidence: 12 · Source: AGT-native: agent-sre
EvaluationEngine": a count of nothing in particular, and a tag that says who
computed the number where a reader expects the data it came from. No row led
to the investigations, the agents or the reviews behind it.

Each row now says what it is measured from, how much of it there was and who
acts when it is off target; a row opens the investigations, model calls and
recommendations behind it, each naming its agent and linking to its review;
and who calculated a number is said in words. A review that is already
decided opens from such a link read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import tenacity

pytest.importorskip("streamlit", reason="the page renders through Streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

import src.llm_client as llm  # noqa: E402
from src import governance, readiness, storage  # noqa: E402
from src.auth import Decision  # noqa: E402
from src.execution_gate import decide_staged_action  # noqa: E402
from src.models import CriticResult  # noqa: E402
from tests.test_evaluation import _investigate, _officer  # noqa: E402

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
PAGE = DASHBOARD / "pages" / "6_Agent_Evaluation.py"
REVIEW_PAGE = DASHBOARD / "pages" / "3_AI_Critic_and_Review.py"
TAGS = ("AGT-native", "AGT-compatible", "Sentinel-specific", "AGT")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    storage.override_db_path(":memory:")
    governance.reload_policy()
    monkeypatch.setattr(llm.LiteLLMClient._acall_with_retries.retry, "wait", tenacity.wait_none())
    yield
    storage.override_db_path(":memory:")


def _run(page: Path = PAGE, **query: str) -> AppTest:
    at = AppTest.from_file(str(page), default_timeout=90)
    for key, value in query.items():
        at.query_params[key] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _html(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def _health(at: AppTest):
    [health] = [d.value for d in at.dataframe if "Dimension" in d.value.columns]
    return health


# ── What the objectives file says about each number ────────────────────────────

def test_every_objective_says_what_it_is_measured_from_and_what_each_role_does():
    _, objectives = readiness.load_spec()
    for o in objectives:
        assert o.data and o.watch_officer and o.system_engineer, f"{o.name}: a number nobody can act on"
        assert not any(tag in f"{o.label} {o.data} {o.watch_officer} {o.system_engineer}" for tag in (*TAGS, "ECE")), o.name
    calibration = next(o for o in objectives if o.name == "calibration_error")
    assert calibration.label == "Confidence calibration" and "how often operators agreed" in calibration.description


def test_who_calculated_a_number_is_said_in_words():
    if str(DASHBOARD) not in sys.path:
        sys.path.insert(0, str(DASHBOARD))
    from lib.agent_eval import CALCULATED_BY_HELP, calculated_by

    assert calculated_by("AGT-native: agent-sre ResponseLatency") == "Agent Governance Toolkit (agent-sre ResponseLatency)"
    assert calculated_by("Sentinel-specific") == "Sentinel's own check"
    assert calculated_by("Sentinel-specific (evidence-id traceability)") == "Sentinel's own check (evidence-id traceability)"
    assert calculated_by("AGT-compatible: ECE (AGT bug G)") == \
        "Sentinel's calculation of a toolkit metric (expected calibration error (the toolkit's own has a known defect: bug G))"
    assert calculated_by("something else") == "something else"
    assert all(words in CALCULATED_BY_HELP for words in
               ("Agent Governance Toolkit", "Sentinel's calculation of a toolkit metric", "Sentinel's own check"))


# ── The page ─────────────────────────────────────────────────────────────────

async def test_each_health_row_says_its_data_its_evidence_and_who_acts():
    at = _run()

    health = _health(at)
    assert list(health.columns) == ["", "Dimension", "Current", "Target", "Status", "Evidence",
                                    "Acts when off target", "Source"]
    rows = {r["Dimension"]: r for r in health.to_dict("records")}
    assert rows["Evidence grounding"]["Source"].startswith("The grounding gate's check of every claim a model made")
    assert rows["Evidence grounding"]["Evidence"] == "no investigations with model output in the window"
    assert rows["Evidence grounding"]["Acts when off target"] == "Watch officer, system engineer"
    assert rows["Cost / investigation"]["Acts when off target"] == "System engineer"
    assert "Confidence calibration" in rows and "Confidence calibration (ECE)" not in rows


async def test_no_tag_is_left_on_the_page_for_a_reader_to_decode(monkeypatch):
    alert, action, _ = await _investigate(monkeypatch, use_llm=True)
    await storage.save_critic_result(CriticResult(alert_id=alert.alert_id, confidence=0.9, escalate=True,
                                                  pattern="ISOLATED_FAULT", reasoning="r"))
    await decide_staged_action(action.action_id, Decision.REJECT, _officer(), "sensor artefact")

    at = _run(dimension="calibration_error")

    shown = "\n".join([_html(at), *(c.value for c in at.caption),
                       *(d.value.to_csv() for d in at.dataframe)])
    for tag in TAGS:
        assert tag not in shown, tag
    assert shown.count("ECE") == shown.count("the expected calibration error, ECE"), "the acronym only where it is defined"
    assert "Calculated by:" in shown and "Sentinel&#x27;s calculation of a toolkit metric" in shown


async def test_a_dimension_opens_what_is_behind_it_each_row_naming_its_agent_and_its_review(monkeypatch):
    _, kdca, _ = await _investigate(monkeypatch, use_llm=True, airport="KDCA")
    _, kiad, _ = await _investigate(monkeypatch, use_llm=True, airport="KIAD")
    await decide_staged_action(kiad.action_id, Decision.REJECT, _officer(), "sensor artefact")

    at = _run(dimension="unsupported_claim_rate")

    health = {r["Dimension"]: r for r in _health(at).to_dict("records")}
    assert health["Unsupported claims"]["Evidence"].startswith("12 model claims")
    html = _html(at)
    assert "Behind this number" in html and "<b style" in html and "2 of 12</b> model claims count against it" in html
    assert html.count("1 of 6 model claims rejected by the grounding gate") == 2, "one row per investigation"
    assert "feed diagnostician" in html, "the agent whose claim cited evidence nobody collected"
    for action, status in ((kdca, "open"), (kiad, "rejected")):
        assert f'href="AI_Critic_and_Review?review={action.action_id}"' in html
    assert ">open</span>" in html and ">rejected</span>" in html
    assert "Measured from:" in html and "Every claim a model made, as judged by the grounding gate." in html
    assert "System engineer, when it is off target" in html and "Watch officer, when it is off target" in html


async def test_without_a_choice_the_first_dimension_off_target_is_opened(monkeypatch):
    for airport in ("KDCA", "KIAD", "KBWI", "KADW"):         # 24 model claims: enough to judge the claim rate
        await _investigate(monkeypatch, use_llm=True, airport=airport)

    html = _html(_run())

    off_target = [r for r in (await readiness.build_report(24)).objectives if r.status in ("MISSED", "AT_RISK")]
    assert off_target, "four rejected claims in 24 miss the unsupported-claims limit"
    assert "Behind this number" in html and off_target[0].description[:40] in html.replace("&#x27;", "'")
    assert off_target[0].detail.startswith("16.7% over 24 model claims (objective at most 5.0%)"), \
        "the verdict's reason reads in the table's units, not as 0.1667 against 0.05"


async def test_one_investigation_is_too_little_to_judge_and_the_table_says_how_much_is_needed(monkeypatch):
    await _investigate(monkeypatch, use_llm=True)

    row = {r["Dimension"]: r for r in _health(_run()).to_dict("records")}["Unsupported claims"]

    assert row["Status"] == "too little evidence (6 of 20)" and row["Evidence"].startswith("6 model claims")


async def test_with_nothing_measured_the_page_says_so_instead_of_opening_a_dimension():
    at = _run()
    assert "Behind this number" not in _html(at)
    assert any("Nothing is off target" in c.value for c in at.caption)


# ── A link to a decided review opens the record of it ────────────────────────

async def test_a_decided_review_opens_read_only_from_a_link(monkeypatch):
    _, action, _ = await _investigate(monkeypatch, use_llm=True)
    await decide_staged_action(action.action_id, Decision.REJECT, _officer(), "sensor artefact")

    at = _run(REVIEW_PAGE, review=action.action_id)

    html = _html(at)
    assert any(m.value.startswith("**AI summary:**") for m in at.markdown), "the drawer opened"
    assert "<b>Rejected</b> by jsmith" in html and "Note: sensor artefact" in html
    labels = [b.label for b in at.button]
    assert not {"Approve escalation", "Continue monitoring", "Reject", "Override & sign off"} & set(labels), \
        "decided: nothing is left to decide"
    assert "review" not in at.query_params


async def test_a_link_to_no_review_says_so():
    at = _run(REVIEW_PAGE, review="no-such-review")
    assert any("No review has that id" in i.value for i in at.info)
