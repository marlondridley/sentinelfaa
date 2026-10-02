# 0006. Agents never grade themselves; quality comes from independent evidence

Status: Accepted (recorded 2026-10-02)

## Context
A model's statement about its own confidence is not evidence. To say whether the
AI layer is fit to deploy, every quality, cost, latency and reliability figure
must come from something the agents do not control.

## Decision
Model output is untrusted until it is checked, and measures are computed from
independent sources:
- **Grounding gate.** Every claim must cite evidence ids that exist in the
  investigation; unsupported claims are rejected and counted
  (`ground()` in `src/investigation/specialists.py`).
- **Critic output guardrail** judges the Critic's answer before it is stored.
- **Independent evidence for evaluation:** the grounding gate's result, the
  operator's recorded decision, the gateway's token and cost figures, the sealed
  record's hash, and the append-only audit log (`src/evaluation.py`).
- **Sealed records.** Each investigation is stored with a SHA-256 verified on
  every read; an integrity failure forces a NOT READY verdict.
- **Readiness verdict** from objectives in `policies/model-slos.yaml`; `--gate`
  turns it into a deployment gate. A figure that cannot be computed honestly
  reads "no data" rather than a guess.
- **Use the vendor library where it is right, and pin where it is wrong.** AGT
  supplies most metrics; the 13 places it computes incorrectly are strict xfail
  tests, so the suite fails the day AGT fixes one and the workaround is retired
  (`tests/test_agt_known_bugs.py`).

## Alternatives considered
- **LLM-as-judge scoring.** Rejected as the primary measure: the judge shares
  failure modes with the thing judged. Not used to grade agents.
- **Self-reported confidence.** Rejected: uncalibrated; calibration (ECE) is
  measured against outcomes instead.
- **Manual review only.** Does not scale and leaves no repeatable gate.

## Consequences
- A deployment gate that can say NOT READY, with the evidence for it.
- More code than adopting the vendor's metrics wholesale, accepted because of the
  documented bugs in them.

## Evidence
`tests/test_evaluation.py` (real workflow, scripted model, exact values);
`tests/test_agt_known_bugs.py`; `tests/test_investigation.py`;
suite of 1835 passing tests with 96% coverage on `src/` as of 2026-10-02. See
[agent-metrics.md](../agent-metrics.md).

## Limits
- Grounding proves a cited id exists, not that the conclusion drawn from it is
  right. The operator's decision is the final check, and overrides are measured.
- Accuracy figures depend on volume of recorded decisions; on a single-feed beta
  the sample is small and the readiness verdict can read INSUFFICIENT DATA.
