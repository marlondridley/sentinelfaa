"""
Re-run the Critic on pending reviews whose verdict has no written summary.

    python scripts/backfill_critic.py            # list what would be reviewed, call nothing
    python scripts/backfill_critic.py --apply    # one model call per review; stores the result

A review is picked when it is PENDING and its alert has no stored verdict or a
verdict without a summary (stored before summaries existed, or never
reviewed). The Critic reads the feed as it was when the alert fired
(build_alert_context(as_of=triggered_at)), so an old alert is judged on its
own evidence, not on today's silence. The stored result replaces the alert's
earlier one; the review, its ticket and its audit trail are untouched -- the
Critic recommends, a person still decides.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import storage  # noqa: E402
from src.critic_agent import CriticAgent  # noqa: E402
from src.models import AlertEvent  # noqa: E402


async def _wanted() -> list[tuple[object, dict]]:
    """(review, alert row) for each pending review still missing a summary."""
    actions = await storage.get_pending_staged_actions()
    alert_ids = [a.alert_id for a in actions if a.alert_id]
    verdicts: dict[str, dict] = {}
    for v in await storage.get_critic_verdicts_for_alerts(alert_ids):
        verdicts.setdefault(v["alert_id"], v)
    # By id, not from the newest N alerts: a review can be older than any cap.
    alerts = {a["alert_id"]: a for a in await storage.get_alerts_by_ids(alert_ids)}
    return [
        (a, alerts[a.alert_id]) for a in actions
        if a.alert_id in alerts and not (verdicts.get(a.alert_id) or {}).get("bluf")
    ]


async def main(apply: bool) -> int:
    wanted = await _wanted()
    print(f"{len(wanted)} pending review(s) without an AI summary")
    for action, row in wanted:
        print(f"  {action.action_id[:8]}  {row['alert_type']:<22} {row.get('airport_code') or '-':<5} "
              f"fired {row['triggered_at']}")
    if not apply or not wanted:
        if wanted:
            print("dry run: nothing called or stored; add --apply to run the Critic")
        return 0

    critic = CriticAgent()
    done = 0
    for action, row in wanted:
        alert = AlertEvent(**{k: row[k] for k in AlertEvent.model_fields if k in row and row[k] is not None})
        result = await critic.critique(alert)
        if result.confidence is None:
            print(f"  {action.action_id[:8]}  no verdict: {result.reasoning}")
            continue          # a degraded answer must not overwrite an earlier real one
        await storage.save_critic_result(result)
        done += 1
        print(f"  {action.action_id[:8]}  {result.confidence:.0%} escalate={result.escalate}  {result.bluf or '(no summary)'}")
    print(f"{done} of {len(wanted)} reviewed")
    return 0 if done == len(wanted) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="call the model and store results")
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
