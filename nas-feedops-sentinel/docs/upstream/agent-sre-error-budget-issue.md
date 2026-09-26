# Draft issue for microsoft/agent-governance-toolkit

Not filed. This is a draft for the repository owner to file. The evidence is
`nas-feedops-sentinel/tests/test_agt_known_bugs.py`, where each bug is a
strict xfail against agent-sre / agent-os-kernel 3.7.0. The appendix lists
the other findings, which could be filed separately or together.

---

**Title:** agent-sre `ErrorBudget`: one failure in 100 events reports an exhausted budget, and `burn_rate()` is off by the window length in seconds

## Summary

`ErrorBudget` measures the budget as a fraction (`total = 1 - target`). But:
- `record_event()` adds a whole `1.0` to `consumed` for every bad event;
- `burn_rate()` divides the allowed error rate by the window length in seconds.

The result: one failed call out of 100, against a 95% objective, reports the
budget as fully consumed and exhausted. The burn rate reads **518,400** instead
of **0.2**. `SLO.evaluate()` then reports `EXHAUSTED` for an agent running at
99% success. `MetricsExporter.record_slo_from_object()` exports those values,
and so does `FleetManager` for agents registered with an SLO.

## Environment

- agent-sre 3.7.0, the latest on PyPI at the time of writing
- Python 3.12, Linux x86_64 and arm64 (pure-Python wheel)

## Reproduction

```python
# pip install agent-sre==3.7.0
from agent_sre.slo.indicators import TaskSuccessRate
from agent_sre.slo.objectives import SLO, ErrorBudget

TARGET = 0.95                      # 95% of calls must succeed: a 5% error budget
budget = ErrorBudget(total=1 - TARGET)
for i in range(100):               # 99 good calls, 1 bad: a 1% error rate
    budget.record_event(good=i != 0)

print("consumed:", budget.consumed)          # expected 0.01 (1 bad of 100 events)
print("remaining:", budget.remaining)        # expected 0.8  (1% of a 5% budget used)
print("is_exhausted:", budget.is_exhausted)  # expected False
print("burn_rate():", budget.burn_rate())    # expected 0.2  (1% actual / 5% allowed)

slo = SLO("availability", indicators=[TaskSuccessRate(target=TARGET)])
for i in range(100):
    slo.record_event(good=i != 0)
print("SLO status:", slo.evaluate().value)   # expected healthy
```

Output with 3.7.0:

```
consumed: 1.0
remaining: 0.0
is_exhausted: True
burn_rate(): 518399.99999999953
SLO status: exhausted
```

## Expected and actual

| | Expected | Actual (3.7.0) |
|---|---|---|
| `consumed` | 0.01 (the observed error rate) | 1.0 |
| `remaining` | 0.8 (1% of a 5% allowance used) | 0.0 |
| `is_exhausted` | False | True |
| `burn_rate()` | 0.2 (actual error rate ÷ allowed error rate) | 518,400 |
| `SLO.evaluate()` | HEALTHY | EXHAUSTED |

The expected values follow the standard definition, as in the Google SRE
workbook: the budget is `1 - target`, budget used is
`observed error rate / (1 - target)`, and burn rate is the same ratio over
the alerting window.

## Cause

`agent_sre/slo/objectives.py`:

```python
total: float = 0.0      # set from the SLO target: 1 - target, a fraction
consumed: float = 0.0

def record_event(self, good: bool) -> None:
    if not good:
        self.consumed += 1.0          # a count, compared against a fraction
    ...

@property
def is_exhausted(self) -> bool:
    return self.consumed >= self.total   # 1.0 >= 0.05 after one failure

def burn_rate(self, window_seconds=None) -> float:
    ...
    actual_error_rate = errors_in_window / total_in_window        # a fraction of events
    allowed_error_rate = self.total / max(self.window_seconds, 1) # a fraction per second
    return actual_error_rate / allowed_error_rate                 # off by window_seconds
```

`consumed` is a count of bad events, while `total` is a fraction of events.
`burn_rate()` compares an error rate per event with an allowance per second.

## Suggested fix

Keep `consumed` in the same unit as `total`, the observed error rate, and compare
rates with rates:

```diff
 @dataclass
 class ErrorBudget:
     total: float = 0.0  # Set from SLO target
     consumed: float = 0.0
+    _bad: int = 0
+    _seen: int = 0

     def record_event(self, good: bool) -> None:
-        if not good:
-            self.consumed += 1.0
+        self._seen += 1
+        if not good:
+            self._bad += 1
+        self.consumed = self._bad / self._seen   # observed error rate, same unit as total
         self._events.append({"good": good, "timestamp": time.monotonic()})

     def burn_rate(self, window_seconds: int | None = None) -> float:
         ...
         actual_error_rate = errors_in_window / total_in_window
-        allowed_error_rate = self.total / max(self.window_seconds, 1)
+        allowed_error_rate = self.total
         if allowed_error_rate <= 0:
             return float("inf") if errors_in_window > 0 else 0.0
         return actual_error_rate / allowed_error_rate
```

`remaining` and `is_exhausted` are then correct unchanged: `1 - 0.01 / 0.05 = 0.8`
and `0.01 >= 0.05` is False. A regression test:

```python
def test_one_failure_in_a_hundred_against_95_percent_uses_a_fifth_of_the_budget():
    budget = ErrorBudget(total=0.05)
    for i in range(100):
        budget.record_event(good=i != 0)
    assert budget.remaining == pytest.approx(0.8)
    assert not budget.is_exhausted
    assert budget.burn_rate() == pytest.approx(0.2)
```

`clear_events()` documents that it does not reset `consumed`. With this fix
it should reset the counters too, or the docstring should say that the rate
covers every event since construction.

## Impact

Any deployment gating on `SLO.evaluate()`, `ErrorBudget.is_exhausted` or the
exported `agent.sre.error_budget.remaining` / `agent.sre.burn_rate` gauges
reports a healthy agent as out of budget, and pages for it. We measure
readiness with our own error-budget arithmetic until this is fixed.

---

## Appendix: other findings in agent-sre and agent-os-kernel 3.7.0

Each has a failing test in `tests/test_agt_known_bugs.py`, shown there next
to the workaround.

| | Component | Finding | Minimal reproduction |
|---|---|---|---|
| C | agent-sre `TaskSuccessRate`, `ToolCallAccuracy`, `PolicyCompliance`, `HallucinationRate` | Each event records the running rate, and `current_value()` averages those running rates, so the result depends on event order | 1 failure then 99 successes: `TaskSuccessRate.current_value()` is 0.948 (true 0.99); `HallucinationRate.current_value()` is 0.052 (true 0.01). `collect()` is correct. |
| D | agent-sre `CostPerTask` | `current_value()` averages running averages | $1.00 then 99 × $0: 0.052 (true 0.01) |
| E | agent-sre `ResponseLatency` | `current_value()` reads every latency ever recorded, not the window's | Record 100 ms, advance the clock past the window: `values_in_window()` is empty, `current_value()` is 100.0 |
| F | agent-sre `ResponseLatency`, `CostPerTask` | Both inherit `compliance()` with "value ≥ target is good", which is inverted for lower-is-better SLIs | `ResponseLatency(target_ms=5000)` with one 100 ms call: compliance 0.0; `CostPerTask(target_usd=0.5)` with one $0.10 task: compliance 0.0 |
| G | agent-sre `CalibrationDeltaSLI` | It compares mean confidence with mean success over all predictions, so over- and under-confidence cancel, contrary to its own docstring (per-claim calibration) | 50 × (1.0, fail) + 50 × (0.0, success): delta 0.0 ("perfectly calibrated"); ECE is 1.0 |
| H | agent-sre `EvaluationEngine` | `pass_rate()` and `average_score()` return 0.0 with no evaluations, which is indistinguishable from all-failed | A fresh engine: 0.0, 0.0 |
| I | agent-sre `EvaluationEngine.get_stats()` | `by_criterion` leaves out any criterion whose average is 0 (`if self.average_score(c) > 0`) | One criterion scoring 0.0 on every run is missing from `by_criterion` |
| J | agent-sre `EvaluationEngine.run()` | A judge exception becomes ABSTAIN, and ABSTAIN on a required criterion does not fail the report (fails open) | A judge that raises on the required SAFETY criterion: `overall_pass` is True |
| K | agent-os-kernel `PolicyDocument` | Unknown keys are ignored, so `rule:` for `rules:` loads a policy with no rules | `PolicyDocument.model_validate({"name": "x", "defaults": {"action": "deny"}, "rule": []})` succeeds |
| L | agent-os-kernel `PolicyDocument` | `defaults.action` defaults to `allow`, so a policy without `defaults` allows everything no rule matches | `PolicyDocument.model_validate({"name": "x"}).defaults.action` is `allow` |
| M | agent-os-kernel `PolicyEvaluator` | `PolicyDocument.tool_allowlist` is documented as "Enforced host-side by the PolicyEvaluator before any sandbox call", and the evaluator never reads it | An allow-by-default document with `tool_allowlist: [read_alert_context]` allows `{"tool_name": "delete_everything"}` |
