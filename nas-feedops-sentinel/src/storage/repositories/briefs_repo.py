"""
Repository for Watch Officer briefs (`watch_officer_briefs` table) -- the periodic
narrative summaries the Watch Officer agent generates.
"""
from __future__ import annotations

import json

import asyncpg

from src.models import WatchOfficerBrief


def _row_to_brief(row) -> WatchOfficerBrief:
    d = dict(row)
    return WatchOfficerBrief(
        brief_id=d["brief_id"],
        generated_at=d["generated_at"],
        posture=d.get("posture", "UNKNOWN"),
        trust_assessment=d.get("trust_assessment", "UNKNOWN"),
        headline=d.get("headline") or "",
        what_changed=json.loads(d.get("what_changed_json") or "[]") if isinstance(d.get("what_changed_json"), str) else d.get("what_changed_json", []),
        why_it_matters=json.loads(d.get("why_it_matters_json") or "[]") if isinstance(d.get("why_it_matters_json"), str) else d.get("why_it_matters_json", []),
        recommended_human_review=json.loads(d.get("recommended_human_review_json") or "[]") if isinstance(d.get("recommended_human_review_json"), str) else d.get("recommended_human_review_json", []),
        evidence=json.loads(d.get("evidence_json") or "[]") if isinstance(d.get("evidence_json"), str) else d.get("evidence_json", []),
        affected_airports=json.loads(d.get("affected_airports_json") or "[]") if isinstance(d.get("affected_airports_json"), str) else d.get("affected_airports_json", []),
        pending_actions_count=d.get("pending_actions_count") or 0,
        critical_alert_count=d.get("critical_alert_count") or 0,
        confidence=d.get("confidence") or 0.5,
        llm_used=bool(d.get("llm_used")),
        limitations=json.loads(d.get("limitations_json") or "[]") if isinstance(d.get("limitations_json"), str) else d.get("limitations_json", []),
    )


class BriefsRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def save(self, brief: WatchOfficerBrief) -> WatchOfficerBrief:
        query = """
            INSERT INTO watch_officer_briefs (
                brief_id, generated_at, posture, trust_assessment, headline,
                what_changed_json, why_it_matters_json, recommended_human_review_json,
                evidence_json, affected_airports_json,
                pending_actions_count, critical_alert_count, confidence,
                llm_used, limitations_json
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (brief_id) DO UPDATE SET
                generated_at = EXCLUDED.generated_at,
                posture = EXCLUDED.posture,
                trust_assessment = EXCLUDED.trust_assessment,
                headline = EXCLUDED.headline,
                what_changed_json = EXCLUDED.what_changed_json,
                why_it_matters_json = EXCLUDED.why_it_matters_json,
                recommended_human_review_json = EXCLUDED.recommended_human_review_json,
                evidence_json = EXCLUDED.evidence_json,
                affected_airports_json = EXCLUDED.affected_airports_json,
                pending_actions_count = EXCLUDED.pending_actions_count,
                critical_alert_count = EXCLUDED.critical_alert_count,
                confidence = EXCLUDED.confidence,
                llm_used = EXCLUDED.llm_used,
                limitations_json = EXCLUDED.limitations_json;
        """
        await self._pool.execute(
            query,
            brief.brief_id, brief.generated_at, brief.posture, brief.trust_assessment, brief.headline,
            json.dumps(brief.what_changed), json.dumps(brief.why_it_matters), json.dumps(brief.recommended_human_review),
            json.dumps(brief.evidence), json.dumps(brief.affected_airports), brief.pending_actions_count,
            brief.critical_alert_count, brief.confidence, bool(brief.llm_used), json.dumps(brief.limitations),
        )
        return brief

    async def get_latest(self) -> WatchOfficerBrief | None:
        row = await self._pool.fetchrow(
            """SELECT brief_id, generated_at, posture, trust_assessment, headline,
                      what_changed_json, why_it_matters_json, recommended_human_review_json,
                      evidence_json, affected_airports_json, pending_actions_count,
                      critical_alert_count, confidence, llm_used, limitations_json
               FROM watch_officer_briefs ORDER BY generated_at DESC LIMIT 1"""
        )
        return _row_to_brief(row) if row else None

    async def get_recent(self, limit: int = 10) -> list[WatchOfficerBrief]:
        rows = await self._pool.fetch(
            """SELECT brief_id, generated_at, posture, trust_assessment, headline,
                      what_changed_json, why_it_matters_json, recommended_human_review_json,
                      evidence_json, affected_airports_json, pending_actions_count,
                      critical_alert_count, confidence, llm_used, limitations_json
               FROM watch_officer_briefs ORDER BY generated_at DESC LIMIT $1""",
            limit,
        )
        return [_row_to_brief(r) for r in rows]
