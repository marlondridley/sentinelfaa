"""
Render the Watch Officer's brief (dashboard/app.py).

Read in ten seconds by someone deciding what to do, so it has one fixed shape:

    STATUS, in a colour                                   time
    the bottom line, one or two sentences
    confidence in the diagnosis of our own data
    DO NOW    at most three actions, each with an owner and a "done when"
    WATCH     what to keep an eye on
    Details   what changed, why it matters, evidence, incidents, limitations and checks

The status, bottom line and actions come from the rules in
src/watch_officer_assessment.py; a model only writes the prose under Details.
A brief stored before that existed has no assessment and keeps the older layout.
"""
from __future__ import annotations

import streamlit as st

from lib import theme
from src.models import WatchOfficerBrief

_TONE = {"RED": "bad", "AMBER": "warn", "GREEN": "ok", "GREY": "neutral"}


def _e(value: object) -> str:
    return theme._esc(value)


def summary_html(brief: WatchOfficerBrief) -> str:
    """The status band, the bottom line and the confidence: everything read first."""
    a = brief.assessment
    fg, border, wash = theme.TONES[_TONE.get(a.status, "neutral")]
    hint = theme.help_hint(
        "How sure this brief is about the state of our own data, not about the airspace. " + a.confidence_reason,
        label="What this means")
    return (
        f'<div style="border:1px solid {border};border-radius:4px;overflow:hidden;margin:2px 0 14px 0;">'
        f'<div style="background:{wash};border-bottom:1px solid {border};padding:14px 20px;display:flex;'
        f'align-items:baseline;gap:6px 16px;flex-wrap:wrap;">'
        f'<span style="font-size:21px;font-weight:800;letter-spacing:0.01em;color:{fg};">{_e(a.status_line)}</span>'
        f'<span class="mono" style="margin-left:auto;font-size:14px;color:{theme.GRAY3};">'
        f'{brief.generated_at:%Y-%m-%d %H:%M} UTC</span></div>'
        f'<div style="background:{theme.DARK_GRAY1};padding:16px 20px;">'
        f'<div style="font-size:18px;font-weight:600;line-height:1.5;color:{theme.LIGHT_GRAY5};">{_e(a.bluf)}</div>'
        f'<div style="margin-top:12px;font-size:15px;color:{theme.GRAY3};">Confidence '
        f'<b style="color:{theme.GRAY5};">{_e(a.confidence_label)}</b> in this diagnosis{hint}</div>'
        f'</div></div>'
    )


def actions_html(brief: WatchOfficerBrief) -> str:
    a = brief.assessment
    if not a.actions:
        return (f'<div class="ev-title">Do now</div><div style="font-size:16px;color:{theme.GRAY5};margin:6px 0 4px 0;">'
                "Nothing. No action is needed.</div>")
    items = "".join(
        f'<li style="margin:0 0 12px 0;">'
        f'<div style="font-size:16px;font-weight:600;line-height:1.4;color:{theme.LIGHT_GRAY5};">{_e(x.action)}</div>'
        f'<div style="font-size:14px;color:{theme.GRAY3};margin-top:3px;">Owner: <b style="color:{theme.GRAY5};">'
        f'{_e(x.owner)}</b> &middot; Done when: {_e(x.done_when)}</div></li>'
        for x in a.actions)
    return (f'<div class="ev-title">Do now</div>'
            f'<ol style="margin:8px 0 2px 0;padding-left:24px;color:{theme.GRAY5};">{items}</ol>')


def _incident_lines(brief: WatchOfficerBrief) -> list[str]:
    lines = []
    for i in brief.assessment.incidents:
        tag = "sensor artifact" if i.artifact else ("history" if i.historical else "current")
        lines.append(f"{i.started_at:%Y-%m-%d %H:%M} UTC · {', '.join(i.airports) or '?'} · "
                     f"{i.alert_count} alert(s) ({', '.join(i.alert_types)}) · {i.reviews} review(s) · {tag}")
    return lines


def render(brief: WatchOfficerBrief) -> None:
    """The brief, in its fixed shape; the older layout for a brief stored without an assessment."""
    a = brief.assessment
    if a is None:
        render_legacy(brief)
        return
    st.markdown(summary_html(brief), unsafe_allow_html=True)
    st.markdown(actions_html(brief), unsafe_allow_html=True)
    theme.evidence_list("Watch", a.watch, limit=3)
    with st.expander("Details: what changed, why it matters, evidence, limitations"):
        prov = "LLM-ASSISTED" if brief.llm_used else "RULE-BASED"
        prov_tone = "info" if brief.llm_used else "neutral"
        st.markdown(
            '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;">'
            + theme.status_chip(brief.posture) + f'<span style="font-size:14px;color:{theme.GRAY3};">trust</span>'
            + theme.status_chip(brief.trust_assessment, size="sm")
            + '<span class="chip" style="color:' + theme.TONES[prov_tone][0] + ";border-color:" + theme.TONES[prov_tone][1]
            + f';padding:2px 8px;font-size:13px;">{prov}</span>'
            + f'<span style="font-size:14px;color:{theme.GRAY3};">{a.above_fold_words} words above the fold</span></div>',
            unsafe_allow_html=True)
        cols = st.columns(2)
        with cols[0]:
            theme.evidence_list("What changed", brief.what_changed, limit=6)
            theme.evidence_list("Incidents (alerts that began together are one)", _incident_lines(brief), limit=5)
        with cols[1]:
            theme.evidence_list("Why it matters", brief.why_it_matters, limit=5)
            theme.evidence_list("Evidence", brief.evidence, limit=5)
        if brief.affected_airports:
            st.markdown(
                '<div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">'
                '<span class="ev-title" style="margin:0;">Affected now</span>'
                + "".join('<a href="' + theme.drill_href("health", airport=x) + '" target="_self" '
                          'style="text-decoration:none;">' + theme.status_chip(x, size="sm") + "</a>"
                          for x in brief.affected_airports)
                + "</div>", unsafe_allow_html=True)
        theme.evidence_list("Limitations and checks", brief.limitations, limit=10)


def render_legacy(brief: WatchOfficerBrief) -> None:
    """A brief stored before the assessment existed."""
    prov = "LLM-ASSISTED" if brief.llm_used else "DETERMINISTIC"
    prov_tone = "info" if brief.llm_used else "neutral"
    st.markdown(
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;">'
        + theme.status_chip(brief.posture)
        + f'<span style="font-size:14px;color:{theme.GRAY3};">trust</span>'
        + theme.status_chip(brief.trust_assessment, size="sm")
        + f'<span class="mono" style="font-size:14px;color:{theme.GRAY3};">confidence {brief.confidence:.0%}</span>'
        + '<span class="chip" style="color:' + theme.TONES[prov_tone][0] + ";border-color:" + theme.TONES[prov_tone][1]
        + f';padding:2px 8px;font-size:13px;">{prov}</span>'
        + f'<span class="mono" style="margin-left:auto;font-size:14px;color:{theme.GRAY3};">'
        + brief.generated_at.strftime("%Y-%m-%d %H:%M:%S") + " UTC</span></div>"
        f'<div style="font-size:17px;font-weight:600;color:{theme.LIGHT_GRAY5};line-height:1.5;">{_e(brief.headline)}</div>',
        unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        theme.evidence_list("What changed", brief.what_changed)
        theme.evidence_list("Recommended human review", brief.recommended_human_review, limit=5)
    with c2:
        theme.evidence_list("Why it matters", brief.why_it_matters, limit=5)
        theme.evidence_list("Evidence", brief.evidence, limit=5)
    if brief.limitations:
        with st.expander("What this brief does not establish"):
            theme.evidence_list("Limitations", brief.limitations, limit=10)
