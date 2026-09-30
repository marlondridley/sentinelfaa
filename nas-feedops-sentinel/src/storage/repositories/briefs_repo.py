"""
Repository for Watch Officer briefs (`watch_officer_briefs` table) -- the periodic
narrative summaries the Watch Officer agent generates.
"""
from __future__ import annotations

import json

import asyncpg

from src.logging_config import get_logger
from src.models import BriefAssessment, WatchOfficerBrief

_log = get_logger(__name__)


def _assessment(value) -> BriefAssessment | None:
    """The stored assessment, or None for a brief written before there was one (or one that no longer validates)."""
    if not value:
        return None
    try:
        return BriefAssessment.model_validate(json.loads(value) if isinstance(value, str) else value)
    except Exception:  # noqa: BLE001 -- an old shape must not stop the dashboard loading its latest brief
        return None


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
        assessment=_assessment(d.get("assessment_json")),
    )


# Columns that existed before the assessment; assessment_json arrived with a later migration.
_LEGACY_COLUMNS = (
    "brief_id", "generated_at", "posture", "trust_assessment", "headline",
    "what_changed_json", "why_it_matters_json", "recommended_human_review_json",
    "evidence_json", "affected_airports_json", "pending_actions_count",
    "critical_alert_count", "confidence", "llm_used", "limitations_json",
)
_COLUMNS = (*_LEGACY_COLUMNS, "assessment_json")


def _save_sql(columns: tuple[str, ...]) -> str:
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    updates = ",\n                ".join(f"{c} = EXCLUDED.{c}" for c in columns[1:])
    return (f"INSERT INTO watch_officer_briefs ({', '.join(columns)}) VALUES ({placeholders})\n"
            f"            ON CONFLICT (brief_id) DO UPDATE SET\n                {updates};")


def _select_sql(columns: tuple[str, ...], tail: str) -> str:
    return f"SELECT {', '.join(columns)} FROM watch_officer_briefs ORDER BY generated_at DESC {tail}"


class BriefsRepository:
    """
    A rolling deploy can run this code against a database the assessment migration has not reached
    yet (the app is rebuilt without its migrate job). Then the assessment column is left out, with
    a warning, and the brief still saves and loads (in the older layout) instead of the Overview
    page failing on a missing column.
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def save(self, brief: WatchOfficerBrief) -> WatchOfficerBrief:
        values = (
            brief.brief_id, brief.generated_at, brief.posture, brief.trust_assessment, brief.headline,
            json.dumps(brief.what_changed), json.dumps(brief.why_it_matters), json.dumps(brief.recommended_human_review),
            json.dumps(brief.evidence), json.dumps(brief.affected_airports), brief.pending_actions_count,
            brief.critical_alert_count, brief.confidence, bool(brief.llm_used), json.dumps(brief.limitations),
        )
        assessment = brief.assessment.model_dump_json() if brief.assessment else None
        try:
            await self._pool.execute(_save_sql(_COLUMNS), *values, assessment)
        except asyncpg.UndefinedColumnError:
            _log.warning("watch_officer_briefs_assessment_column_missing_run_migrations")
            await self._pool.execute(_save_sql(_LEGACY_COLUMNS), *values)
        return brief

    async def _fetch(self, tail: str, *args, one: bool = False):
        for columns in (_COLUMNS, _LEGACY_COLUMNS):
            try:
                query = _select_sql(columns, tail)
                return await (self._pool.fetchrow(query, *args) if one else self._pool.fetch(query, *args))
            except asyncpg.UndefinedColumnError:
                _log.warning("watch_officer_briefs_assessment_column_missing_run_migrations")
        raise RuntimeError("watch_officer_briefs is missing columns beyond assessment_json")

    async def get_latest(self) -> WatchOfficerBrief | None:
        row = await self._fetch("LIMIT 1", one=True)
        return _row_to_brief(row) if row else None

    async def get_recent(self, limit: int = 10) -> list[WatchOfficerBrief]:
        rows = await self._fetch("LIMIT $1", limit)
        return [_row_to_brief(r) for r in rows]
