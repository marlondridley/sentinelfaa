# Operator playbook

How the people who use Sentinel work with it:
- the watch officer and supervisor, who decide;
- the auditor, who reads;
- the engineer, program manager or governance reviewer, who assesses the AI
  layer.

For what to do when something breaks, see `docs/runbooks.md`. To bring the
stack up, see `docs/operations.md`.

## What Sentinel is for, and what it is not

Sentinel watches the trustworthiness of the live FAA SWIM/STDDS picture for
PCT TRACON (KDCA, KIAD, KBWI, KADW) and puts CRITICAL problems in front of a
person, with an investigation behind each one.
- It never acts on the NAS.
- It never decides.
- The AI layer explains and challenges; the rules raise and stage alerts;
  a person approves, rejects or overrides.

## Roles

| Keycloak role | May | May not |
|---|---|---|
| `watch_officer` | approve, reject, mark monitoring | override the recommendation |
| `supervisor` | all of the above, and override (edit the recommendation or severity before sign-off) | nothing more; no role executes anything |
| `auditor` | read every decision, investigation and evaluation | decide anything |
| signed in, no role | read | decide anything |

Every decision is recorded with the person's id, their roles at that moment,
and how their identity was verified (`[oidc]`, `[api_key]`; decisions before
2026-09-26 show `[keycloak-password]`, the retired in-app sign-in). A `[self-declared]` identity appears only when the deployment
allows unverified decisions; the evaluation flags it.

## Start of shift

1. **Dashboard.**
   - Read the verdict first (GREEN, AMBER, RED or NO DATA). Rules compute
     it from schema conformance, ingest lag and open CRITICAL alerts of the last
     hour, from live data only (replays and synthetic data are never counted);
     no model is consulted.
   - RED means validate against a secondary source before relying on the
     feed.
2. **Facility status.** Is a problem isolated to one airport or sector-wide?
3. **AI investigation card**, five indicators for the latest investigation:
   - *Evidence quality*: the share of the model's claims that cite evidence
     that was collected. "Rules only" means no model contributed.
   - *Confidence*: the Critic's own stated figure. Treat it as a claim, not
     a measurement. Its calibration is measured on Agent Evaluation.
   - *Investigation*: opened to sealed, against the SLA.
   - *Cost*: gateway-reported spend.
   - *Human review*: always Required. The card shows whether the action is
     still pending.
   The coloured status is the worst of that investigation's governance
   gates. Any red means open Agent Evaluation before trusting the AI output
   for the shift.
4. **Review queue.** The Dashboard lists each review awaiting a decision, one
   line each; the line opens that review. The Review Queue page counts the
   same reviews: *Awaiting decision* (nobody has answered), *AI suggests
   escalation* (those of them the Critic would escalate) and *Monitoring*
   (someone answered "continue monitoring": out of the queue, still open).
   Every number counts reviews, one per condition, never readings. The
   `HumanReviewQueueStale` alert fires past one hour.

## Deciding on a staged action

For each review awaiting a decision on the Review Queue:
1. **Read the alert and the recommendation.** The recommendation is the AI's
   suggestion, labelled with its provenance.
2. **Read the investigation's bottom line**, then open it:
   - *Established by the evidence*: each line cites evidence ids you can
     follow.
   - *Contested*: where the specialists and the Red-Team Critic disagree.
     Disagreements are shown, never resolved by vote.
   - *Verify before deciding*: the checks the brief says to make.
   - *Red-Team Critic*: the strongest alternative explanation, and every
     challenge.
3. **Open "How this investigation was evaluated".** These are the governance
   gates. What they mean for you:
   - **Quality**
     - *WARN, model claims grounded*: the model tried to cite evidence that
       does not exist. Those claims were removed before you saw the record,
       but read the rest with more care.
     - *WARN, required sources*: data the rules expect was not available.
       The brief says which.
   - **Safety**
     - *FAIL, advisory only* or *FAIL, no automated decision*: stop, and
       escalate to engineering. This should never happen.
   - **Cost and Latency**: informational for you. They matter to readiness,
     not to this decision.
   - **Audit**
     - *FAIL, sealed record*: the record does not match its hash. Do not
       rely on it; report it.
4. **Decide.**
   - **Approve**: the escalation is warranted.
   - **Reject**: it is not.
   - **Monitor**: keep it pending and watch. The ticket moves to monitoring.
   - **Override** (supervisor): approve with an edited recommendation or
     severity. The AI's original stays in the record next to yours.

   Add a note. Your decision is written atomically to the append-only audit
   log.

Where your decision differs from the Critic's recommendation, that is not an
error. It is the signal the evaluation layer uses to measure decision quality,
missed and false escalations, and calibration. Decide on the evidence.

## When not to trust the AI layer

- The card or the Review Queue shows **"rules only"**. The model was
  unavailable or refused and the agents fell back to their rules. The
  investigation still stands, on rules alone.
- **Evidence quality is well below 95%**, or the Unsupported claims
  dimension is red on Agent Evaluation.
- The Dashboard verdict is **RED**, or the data-origin banner shows **non-live
  data** (replay or synthetic). The Red-Team Critic challenges non-live
  alerts too.
- Agent Evaluation reads **NOT READY**. The rules, the staging and your
  decision are unaffected; the AI's advice is not yet trustworthy enough to
  lean on.

In every one of these cases the rules, the staging and the human gate work
the same. Only the advice is weaker.

## For engineering, program management and governance (Agent Evaluation)

The Agent Evaluation & Governance page is the third layer, not for the
operations floor.

1. **Verdict**:
   - READY;
   - AT RISK (nothing missed, something close to its limit);
   - NOT READY (a dimension missed, or a record failed its integrity check);
   - INSUFFICIENT DATA (too little evidence to say).

   The same verdict gates deployment: `python -m src.readiness --gate`.
2. **Health table**: each dimension, current value, target and status.
   - *Evidence* is how much the number rests on, in its own unit ("30 model
     claims"), and how much of its error budget or limit is used.
   - *Source* is the data it is measured from; *Acts when off target* is
     who does something about it.
   - "Too little evidence (n of m)" is honest: the dimension is not judged
     until there is enough.
   - **Select a row** (the first one off target opens by itself). It shows
     what a watch officer and a system engineer each do about it, and
     *Behind this number*: the investigations, model calls and
     recommendations it was computed from, what counts against it first.
     Each row names its agent and links to its review, open or decided, so
     a number leads to the work: the engineer goes to the agent named, the
     officer opens the review.
   - *Calculated by* says who computed the number: the Agent Governance
     Toolkit, Sentinel's calculation of a toolkit metric (the toolkit's
     own has a known defect), or Sentinel's own check
     (`docs/agent-metrics.md`).
   - The guidance and the data description for each dimension are in
     `policies/model-slos.yaml`, beside its target.
3. **Latency**: the path from alert to investigation to evidence to human
   decision, showing where the time goes; P50/P95/P99 by stage and agent.
4. **Cost**:
   - per invocation, investigation, alert, resolved investigation and
     useful evidence item;
   - by served model;
   - what retries and failures cost.
5. **Reliability**: per agent and per provider; fallback, schema-failure
   and malformed-output rates.
6. **Decision quality**: grounding, accuracy against operator decisions,
   false and missed escalations, overrides, calibration (ECE), and the
   evidence the models added.
7. **Orchestration**: tool calls, handoffs, dead ends, context size by
   agent, depth, and how investigations ended.
8. **Governance gates**: pass rates per gate group, and policy compliance.

Targets live in `policies/model-slos.yaml`. Changing a target, a policy or
a gate limit is a reviewed code change. The tests pin the policy decisions.

## Switching model provider

Sentinel is model-agnostic, though today only Claude Haiku sits behind
the same gateway alias and are measured the same way. To change provider,
follow the runbook "Change the model provider". Watch Agent Evaluation for
the new provider's row under Reliability, and let readiness reach a verdict
on its own evidence before relying on it.
