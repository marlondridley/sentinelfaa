"""
End-to-end proof: one command, one log, the whole path.

    docker compose --profile proof run --rm proof            # local, live model if configured
    python scripts/e2e_proof.py --require-llm                # fail unless the model answered

Against a DISPOSABLE database only (the name must contain test/ci/scratch/tmp;
it is created if missing and wiped first):

  1. Real FAA capture: the ten messages in tests/fixtures/swim_kit/
     capture_2026-08-27 are scanned from a scratch folder. All ten must be
     processed, none quarantined, and the pipeline must accept the events.
  2. Injected fault: the real capture contains no CRITICAL condition, so a
     SYNTHETIC cascade-failure scenario (src/demo_scenario.py, labelled as
     such throughout) is run through the same pipeline. Every CRITICAL alert
     must be staged PENDING, evaluated by the Critic (model or recorded
     degradation), and investigated with a sealed, traceable record.
  3. Authorization: an operator holding only `auditor` tries to approve and
     must be refused with nothing written.
  4. Decision: a `watch_officer` approves one staged action. The action must
     be APPROVED, its ticket APPROVED_FOR_ESCALATION, and the audit log must
     hold (new)->PENDING by system and PENDING->APPROVED attributed with the
     operator's roles.
  5. Evidence integrity: UPDATE and DELETE on the audit log must be refused.

The operator principals in steps 3-4 are synthetic and exist only in the
disposable database; their ids say so. Exit status 0 only if every step passed.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import logging
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CAPTURE = REPO / "tests" / "fixtures" / "swim_kit" / "capture_2026-08-27"


class Proof:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
        self.failures += not ok
        return ok

    @staticmethod
    def step(title: str) -> None:
        print(f"\n== {title}")


async def _ensure_disposable_database() -> None:
    import asyncpg

    from src.config import settings
    from src.storage.test_support import _DISPOSABLE_DB_MARKERS

    name = settings.POSTGRES_DB or ""
    if not any(marker in name.lower() for marker in _DISPOSABLE_DB_MARKERS):
        raise SystemExit(f"refusing to run against database {name!r}: not marked disposable "
                         f"({', '.join(_DISPOSABLE_DB_MARKERS)})")
    conn = await asyncpg.connect(host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
                                 user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD,
                                 database="postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def run(require_llm: bool) -> int:
    from src import storage, swim_file_ingest
    from src.auth import AuthorizationError, Decision, Principal, PrincipalSource
    from src.config import settings
    from src.demo_scenario import generate_cascade_failure_scenario
    from src.execution_gate import decide_staged_action
    from src.pipeline import run_pipeline
    from src.storage.pool import close_db_pool, get_db_pool
    from src.storage.repositories.investigations_repo import InvestigationsRepository
    from src.storage.test_support import reset_db

    proof = Proof()
    print("NAS FeedOps Sentinel -- end-to-end proof")
    print(f"  database: {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")
    print(f"  model:    {'enabled, ' + settings.INVESTIGATION_MODEL if settings.CRITIC_ENABLED else 'disabled (rules only)'}")

    await _ensure_disposable_database()
    await reset_db()
    pool = await get_db_pool()

    # 1 ── Real capture ────────────────────────────────────────────────────────
    proof.step("1. Real FAA SWIM capture (2026-08-27, 10 messages)")
    with tempfile.TemporaryDirectory() as tmp:
        src_dir, processed, failed = Path(tmp) / "swim_live", Path(tmp) / "processed", Path(tmp) / "failed"
        src_dir.mkdir()
        for f in CAPTURE.iterdir():
            shutil.copy(f, src_dir / f.name)
        events, stats = await asyncio.to_thread(
            swim_file_ingest.scan_swim_folder, src_dir, 500, processed, failed, "replay")
        proof.check(stats.files_processed == 10 and stats.files_failed == 0,
                    "all ten captured files ingested, none quarantined",
                    f"processed={stats.files_processed} failed={stats.files_failed} events={len(events)}")
        with contextlib.redirect_stdout(io.StringIO()):
            real = await run_pipeline(events, label="E2E_REAL_CAPTURE")
        proof.check(all(e.data_origin == "replay" for e in events), "replayed capture tagged data_origin=replay")
        proof.check(real["passed"] == real["event_count"] > 0, "every real event passed validation",
                    f"{real['passed']}/{real['event_count']} passed, {real['alert_count']} alert(s), "
                    f"{real['staged_count']} staged")

    # 2 ── Injected fault ──────────────────────────────────────────────────────
    proof.step("2. Injected fault (SYNTHETIC cascade-failure scenario)")
    with contextlib.redirect_stdout(io.StringIO()):
        fault = await run_pipeline(generate_cascade_failure_scenario(), label="E2E_SYNTHETIC_FAULT")
    alerts = await storage.get_recent_alerts(limit=1000)
    critical = [a for a in alerts if a["severity"] == "CRITICAL"]
    proof.check(bool(critical) and all(a["data_origin"] == "synthetic" for a in critical),
                "alerts from the synthetic scenario are tagged data_origin=synthetic")
    pending = await storage.get_pending_staged_actions()
    proof.check(bool(critical) and {p.alert_id for p in pending} >= {a["alert_id"] for a in critical},
                "every CRITICAL alert staged PENDING for human review",
                f"{len(critical)} CRITICAL, {len(pending)} pending")

    critic_rows = await storage.get_recent_critic_results(limit=100)
    by_model = [r for r in critic_rows if r.get("confidence") is not None]
    proof.check(bool(critic_rows), "Critic evaluated the CRITICAL alerts",
                f"{len(critic_rows)} verdict(s): {len(by_model)} from the model, "
                f"{len(critic_rows) - len(by_model)} recorded degradation")
    if require_llm:
        proof.check(bool(by_model), "the model answered (required)",
                    by_model[0]["reasoning"][:120] if by_model else critic_rows[0]["reasoning"][:160])

    repo = InvestigationsRepository(pool)
    invs = [await repo.get_by_action_id(p.action_id) for p in pending]
    complete = [i for i in invs if i and i.status == "COMPLETE"]
    proof.check(len(complete) == len(pending), "every staged action has a complete investigation",
                f"{len(complete)}/{len(pending)}; investigated this pass: {fault['investigated_count']}")
    proof.check(all(i.verify() and not i.untraceable_claims() for i in complete),
                "every investigation is sealed and every claim traces to evidence")
    llm_invs = [i for i in complete if any(s.detail == "llm" for s in i.steps)]
    print(f"         {len(llm_invs)} investigation(s) used the model; "
          f"the rest ran on rules (MAX_LLM_INVESTIGATIONS_PER_PASS={settings.MAX_LLM_INVESTIGATIONS_PER_PASS})")
    if require_llm:
        proof.check(bool(llm_invs), "an investigation used the model (required)")
    if complete:
        brief = complete[0].brief
        print(f"         sample brief: {brief.bottom_line[:150]}")

    # 3 ── Authorization ───────────────────────────────────────────────────────
    proof.step("3. Authorization: an auditor may not decide")
    target = pending[0]
    auditor = Principal(principal_id="e2e-proof-auditor (synthetic)", role="Auditor",
                        source=PrincipalSource.PASSWORD_GRANT, roles=frozenset({"auditor"}))
    audit_before = await storage.get_action_audit_log(action_id=target.action_id)
    try:
        await decide_staged_action(target.action_id, Decision.APPROVE, auditor, "should be refused")
        proof.check(False, "auditor approval refused")
    except AuthorizationError as exc:
        proof.check(True, "auditor approval refused", str(exc))
    proof.check(await storage.get_action_audit_log(action_id=target.action_id) == audit_before,
                "nothing written by the refused attempt")

    # 4 ── Decision ────────────────────────────────────────────────────────────
    proof.step("4. A watch officer approves")
    officer = Principal(principal_id="e2e-proof-officer (synthetic)", role="Watch Officer",
                        source=PrincipalSource.PASSWORD_GRANT, roles=frozenset({"watch_officer"}))
    decided = await decide_staged_action(target.action_id, Decision.APPROVE, officer, "e2e proof approval")
    proof.check(decided is not None and decided.status == "APPROVED", "action APPROVED",
                decided.decided_by if decided else "")
    ticket = await storage.get_ticket_by_action_id(target.action_id)
    proof.check(ticket is not None and ticket.status == "APPROVED_FOR_ESCALATION", "ticket APPROVED_FOR_ESCALATION")
    audit = await storage.get_action_audit_log(action_id=target.action_id)
    transitions = {(r["previous_status"], r["new_status"]): r["actor"] for r in audit}
    proof.check(transitions.get(("(new)", "PENDING")) == "system", "audit: (new) -> PENDING by system")
    approved_by = transitions.get(("PENDING", "APPROVED"), "")
    proof.check("roles=watch_officer" in approved_by, "audit: PENDING -> APPROVED with the operator's roles",
                approved_by)

    # 5 ── Integrity ───────────────────────────────────────────────────────────
    proof.step("5. The audit log is append-only")
    for statement in ("UPDATE action_audit_log SET note = 'tampered'", "DELETE FROM action_audit_log"):
        try:
            await pool.execute(statement)
            proof.check(False, f"refused: {statement}")
        except Exception as exc:  # noqa: BLE001
            proof.check("append-only" in str(exc), f"refused: {statement.split()[0]}", str(exc).splitlines()[0])

    await close_db_pool()
    print(f"\n{'PROOF PASSED' if not proof.failures else f'PROOF FAILED: {proof.failures} check(s)'}")
    return 1 if proof.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--require-llm", action="store_true", help="fail unless the model produced output")
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)
    return asyncio.run(run(args.require_llm))


if __name__ == "__main__":
    raise SystemExit(main())
