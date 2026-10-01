"""
Render a stored Investigation on the Review page.

The reviewer sees the Watch Officer's bottom line first, then -- one click
away -- what the evidence establishes, what is contested, what to verify,
the Red-Team Critic's challenges, and every evidence item a claim cites.
Evidence ids are shown next to each statement so any conclusion can be
followed back to the observation that produced it.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from lib import safe_text, theme
from src.async_bridge import run_async as run
from src.investigation.record import Investigation


async def _load(action_id: str) -> Investigation | None:
    from src.storage.pool import get_db_pool
    from src.storage.repositories.investigations_repo import InvestigationsRepository

    return await InvestigationsRepository(await get_db_pool()).get_by_action_id(action_id)


def load_investigation(action_id: str) -> Investigation | None:
    try:
        return run(_load(action_id))
    except Exception as exc:  # noqa: BLE001 -- a broken record must not hide the queue
        st.warning(safe_text.failure("The investigation record could not be loaded", exc))
        return None


def _ids(evidence_ids: list[str]) -> str:
    if not evidence_ids:
        return ""
    return (f'<span class="mono" style="font-size:13px;color:{theme.GRAY1};"> ⟵ '
            + theme._esc(", ".join(evidence_ids)) + "</span>")


def _line(text: str, evidence_ids: list[str], *, tone: str = "info") -> None:
    st.markdown(
        f'<div style="border-left:2px solid {theme.TONES[tone][1]};padding:2px 0 2px 10px;margin:4px 0;'
        f'font-size:15px;color:{theme.LIGHT_GRAY5};">{theme._esc(text)}{_ids(evidence_ids)}</div>',
        unsafe_allow_html=True,
    )


def render_investigation(inv: Investigation | None) -> None:
    if inv is None:
        st.caption("No investigation recorded for this action yet.")
        return
    brief = inv.brief
    used_model = [s.agent for s in inv.steps if s.agent and s.detail == "llm"]
    method = "model + rules" if used_model else "rules only"
    challenges = inv.critique.challenges if inv.critique else []

    st.markdown(
        '<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin:6px 0 2px 0;">'
        f'<span class="chip" style="color:{theme.BLUE5};border-color:{theme.TONES["info"][1]};'
        'padding:2px 8px;font-size:13px;">INVESTIGATION</span>'
        f'<span style="font-size:15px;color:{theme.LIGHT_GRAY5};">'
        + theme._esc(brief.bottom_line if brief else "Investigation incomplete.") + "</span>"
        f'<span class="mono" style="margin-left:auto;font-size:13px;color:{theme.GRAY1};">'
        + theme._esc(f"{len(inv.evidence)} evidence · {len(challenges)} challenges · {method}") + "</span></div>",
        unsafe_allow_html=True,
    )

    with st.expander("Investigation: evidence, challenges, what to verify"):
        if brief:
            st.markdown(f"**Situation.** {safe_text.plain(brief.situation)}")
            if brief.established:
                st.markdown("**Established by the evidence**")
                for c in brief.established:
                    _line(c.statement, c.evidence_ids, tone="ok")
            if brief.disagreements:
                st.markdown("**Contested**")
                for d in brief.disagreements:
                    positions = ", ".join(f"{k}: {v}" for k, v in d.positions.items())
                    _line(f"{d.statement} ({positions})", d.evidence_ids, tone="warn")
            if brief.verify_next:
                st.markdown("**Verify before deciding**")
                for v in brief.verify_next:
                    _line(f"{v.action} — {v.why}" if v.why else v.action, v.evidence_ids, tone="info")

        if inv.critique:
            st.markdown("**Red-Team Critic**")
            alt = inv.critique.strongest_alternative
            if alt:
                _line(f"Strongest alternative: {alt.statement}", alt.evidence_ids, tone="bad")
            for c in challenges:
                _line(f"{c.challenge_id} {c.kind.replace('_', ' ').lower()}: {c.statement}", c.evidence_ids,
                      tone="bad" if c.kind == "CONTRADICTING_EVIDENCE" else "warn")

        st.markdown("**Specialist findings**")
        for f in inv.findings:
            st.markdown(
                f'<div style="font-size:14px;color:{theme.GRAY3};margin:2px 0;">'
                f'<b style="color:{theme.LIGHT_GRAY5};">{theme._esc(f.agent.replace("_", " ").title())}</b> '
                f'— {theme._esc(f.question)} <span class="mono">{theme._esc(f.conclusion)}</span> '
                f'({theme._esc(f.provenance.method)})</div>',
                unsafe_allow_html=True,
            )
            for c in f.claims:
                _line(c.statement, c.evidence_ids, tone="neutral")

        st.markdown("**Evidence**")
        st.dataframe(
            pd.DataFrame([
                {"evidence_id": e.evidence_id, "kind": e.kind, "subject": e.subject, "source": e.source,
                 "data": json.dumps(e.data, default=str)[:300]}
                for e in inv.evidence
            ]),
            hide_index=True, use_container_width=True,
        )

        verified = inv.verify()
        st.markdown(
            f'<div class="mono" style="font-size:13px;color:{theme.GREEN5 if verified else theme.RED5};">'
            + theme._esc(f"record {inv.investigation_id} · sha256 {(inv.record_sha256 or '')[:16]}… · "
                         + ("integrity verified" if verified else "INTEGRITY CHECK FAILED"))
            + "</div>",
            unsafe_allow_html=True,
        )
        st.dataframe(
            pd.DataFrame([{"step": s.step, "outcome": s.outcome, "detail": s.detail[:160]} for s in inv.steps]),
            hide_index=True, use_container_width=True,
        )
