"""
Run scripts/e2e_proof.py as part of the suite, with the model disabled, so
the whole path -- real capture, staging, investigation, authorization,
approval, audit -- is re-proven on every push.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from src.config import settings


def test_end_to_end_proof_passes(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "e2e_proof", Path(__file__).resolve().parent.parent / "scripts" / "e2e_proof.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(settings, "CRITIC_ENABLED", False)
    try:
        status = module.main([])
    finally:
        logging.disable(logging.NOTSET)
    out = capsys.readouterr().out
    assert status == 0, out
    assert "PROOF PASSED" in out
    assert "[FAIL]" not in out
