"""
Agent metrics: the Agent Governance Toolkit computes them, OpenTelemetry
carries them.

Every agent metric Sentinel reports comes from an AGT component (agent-sre or
agent-os-kernel) unless this module says otherwise, next to the code that
replaces it, with one of two reasons:

  AGT bug       a test in tests/test_agt_known_bugs.py shows AGT returning the
                wrong value. Those tests are strict xfails: when an AGT
                release fixes the bug they start passing, the suite fails,
                and the in-repo code here should be removed in favor of AGT.
  no equivalent AGT has no metric that measures this.

The mapping from every metric in the evaluation design to its source is in
docs/agent-metrics.md.
"""
from __future__ import annotations

from collections.abc import Iterable


def expected_calibration_error(pairs: Iterable[tuple[float, bool]], bins: int = 10) -> tuple[float | None, int]:
    """
    Expected calibration error of stated confidences against outcomes: the
    size-weighted mean, over equal-width confidence bins, of
    |mean stated confidence - observed success rate|. (value, samples);
    value is None with no samples.

    In-repo -- AGT bug: agent-sre's CalibrationDeltaSLI compares the mean
    confidence with the mean success rate over every prediction at once, so
    over- and under-confidence cancel: a model that is certain and always
    wrong half the time, and certain it is wrong and always right the other
    half, reads as perfectly calibrated
    (test_agt_calibration_delta_detects_offsetting_miscalibration).
    """
    counts = [0] * bins
    confidence = [0.0] * bins
    correct = [0.0] * bins
    n = 0
    for stated, outcome in pairs:
        stated = min(max(float(stated), 0.0), 1.0)
        index = min(int(stated * bins), bins - 1)
        counts[index] += 1
        confidence[index] += stated
        correct[index] += 1.0 if outcome else 0.0
        n += 1
    if n == 0:
        return None, 0
    error = sum(
        (counts[i] / n) * abs(confidence[i] / counts[i] - correct[i] / counts[i])
        for i in range(bins) if counts[i]
    )
    return round(error, 6), n
