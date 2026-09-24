"""
Structured multi-agent investigation of a staged CRITICAL alert.

    record.py       the shared Investigation record (evidence, claims, challenges, brief)
    evidence.py     deterministic evidence collection
    specialists.py  Feed Diagnostician, Correlation Analyst, Red-Team Critic, Watch Officer
    workflow.py     the fixed-order orchestrator; the only writer of the record

Advisory only: an investigation never approves, rejects or executes a staged
action. It gives the human reviewer evidence-backed context for that decision.
"""
from src.investigation.record import Investigation
from src.investigation.workflow import investigate_staged, run_investigation

__all__ = ["Investigation", "investigate_staged", "run_investigation"]
