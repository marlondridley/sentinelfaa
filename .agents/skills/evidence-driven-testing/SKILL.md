---
name: evidence-driven-testing
description: Prove that a change works with evidence from a real test run, and report each claim so someone else can check it. Use when building or reviewing a change in this repository, when writing acceptance criteria, when a factory agent records what it tested in build.md or a review, or when deciding whether a claimed result is backed by evidence. Covers the screenshot-or-test decision, test targets as concrete statements, passed, failed and untested results, where screenshots go, and what evidence must never contain.
---

# Evidence-driven testing

**Evidence comes from the real test run.** A test result, a command's output or a screenshot is evidence only when it
came from actually running the change. Never fabricate, simulate, stitch together or stage evidence after the fact and
present it as a live test. A screenshot proves only the state it shows; it does not prove that the steps before it
worked. Tie each claim to a result you observed: a page state, a command's output, a test's pass or fail line.

Choose **the lightest evidence that convincingly verifies the change.** Evidence adds to the repo's gates (ruff, mypy,
pytest, the build, `infra`); it never replaces them.

This skill produces evidence and a test result. It does not authorize a merge or a release: in the factory the approver
decides that, alone (see "Results and the gate" below).

## Words used here

- **Test target**: one behaviour the change must have, written as a statement that can be checked.
- **Proof method**: how a target is proved. A command (a test or a probe), a screenshot, or both.
- **Evidence**: what the proof method produced. A command and its outcome, or the path of a saved screenshot.
- **Evidence folder**: where saved evidence goes (see "Where evidence goes").

## Write the test targets first

Before testing, write each target as a concrete expected behaviour. Good targets:

- "The Review Queue count equals the number of rows under it."
- "A watch officer without the supervisor role cannot approve the action."
- "`sync_agent_skills.py --check` exits 0 after the new skill is added."

Vague targets such as "the page works" or "test the dashboard" cannot pass or fail, so do not write them. In the
factory, the spec's acceptance criteria are the test targets; the spec-writer states the proof method for each one.

For each target, record: the statement, the preconditions that matter, what you did, what you expected and what you
saw, the result (passed, failed or untested), the evidence, and any caveat when the check was partial.

## The screenshot-or-test decision

For every changed feature, answer this question and write the answer down: **can a screenshot alone prove the required
behaviour?**

- **Yes**, for a visible state only: capture the state and check that the image really shows the expected result. If
  the behaviour also has hidden effects (stored data, permissions, what a count is computed from), add a test for those.
- **No**: add or extend an automated test that asserts the behaviour, run it, and keep the command and its outcome as
  the evidence. Run the narrowest test first, then the full suite.
- **Partially**: capture the visible part and test the hidden part. Say exactly what each piece of evidence proves.
- **Cannot run**: say what blocks you and mark the target untested. Do not put in a screenshot of some other state, or
  a claim from reading the code, in its place.

A change with no runtime behaviour (docs, skill or agent text) is proved by commands and file contents: check the
content, the links, and any command the text tells someone to run.

## Tests assert outcomes

- A test must assert the outcome, not just that the code ran. It should fail if the behaviour regresses.
- For a bug fix, show the test failing against the old code first when practical, then passing after the fix. If the
  old failure cannot be reproduced, say why.
- Never weaken or delete a meaningful assertion to get a green run (`AGENTS.md` also forbids skipping a test or turning
  a strict `xfail` into a pass).
- Never claim a test passed unless it actually ran and you read its result: the pytest summary line, not only the exit
  code.

## Running tests

Tests run only inside the Docker test image, never with `pytest` on the host. Use the `ci-like-test-run` skill:
`run-ci-like.sh build` first, then the single-test recipe in `AGENTS.md` for the narrowest test, then
`run-ci-like.sh test` for the suite. Tests run only against the job's own throwaway database (`<WT_NAME>-cipg`), never
any other.

## Browser evidence

Browser evidence goes only through the `agent-browser` CLI (run `agent-browser skills get core` first to learn it).

- Pass `--session <job-id>-<agent>` on every call (for example `--session 007-evidence-driven-testing-builder`), so
  parallel jobs never share a browser. Close the session when you are done.
- Open the dashboard on the port your prompt gives, started as `AGENTS.md` "Start the dev server on a given port"
  describes, with sign-in off. Do not sign in and do not submit forms. There is no test account to prepare.
- Before you assert anything, check that the page comes from the intended build: the container you started, on your
  port, from the image `run-ci-like.sh build` made from your worktree. Record `git rev-parse HEAD`.
- After each meaningful action, look at the resulting state before calling it a pass. A click that completes, or a tool
  that returns without an error, is not a pass.
- Take screenshots at the start, at important changes and at the end, with names that say what they show:
  `01-precondition.png`, `02-filter-applied-passed.png`.

No other capture method: no live screen recording, no computer use, no screen-capture tools, and no Playwright or Node
installed on the host. One desktop runs many jobs at once, so a recording of the screen cannot be kept to one job.

## Evidence that is not a screenshot

- **API or performance**: run a repeatable probe and keep the command, sample size, status codes and the numbers that
  matter, before and after. Never invent an improvement.
- **CLI or data transformation**: keep a representative input and output pair and compare it with the expected result.
- **Agent behaviour**: keep the tool calls and the returned result, plus the check used to judge it. A model grading its
  own output is not proof; corroborate it with a deterministic check.
- **Documentation only**: check the content, the links and any runnable example. No screenshot is needed.
- **Bug fix**: capture the failure before the fix when practical, then the same target after it.

Always check that the process you probed runs the intended code (the port, the container, the commit) before you credit
a result to the change.

## Data and privacy

- Only real FAA SWIM data: replayed captures from `tests/fixtures/` or the live feed (the `real-data-only` skill). Never
  load synthetic, demo or sample data, and never seed data to make a page look right. A page in its empty state is
  evidence of the empty state; say so.
- Evidence must never show secrets: `.env`, keys, certificates, tokens, Vault output. If a check needs one on screen,
  mark it untested and say why.
- Do not copy real flight identifiers into evidence beyond what a target needs.
- The dashboard's look is approved (the `dashboard-rules` skill). Do not restyle it to make a screenshot pass. A count
  that disagrees with its rows is a finding, not something to crop out.

## Where evidence goes

- **In the factory**: `factory/jobs/<job-id>/evidence/round-<N>/<agent>/` in the main checkout, with `<agent>` one of
  `builder`, `ux`, `ui`, and `N` the round you are working on. Create it with `mkdir -p`. The job folder is gitignored
  and outlives the worktree, so the evidence survives the merge. It is never committed.
- **Outside the factory**: `.artifacts/<task-name>/` in your checkout. It is not in `.gitignore`; add it there only if
  the owner asks, and never commit it.

The report is the file you already write: in the factory, `build.md` or your review file. Do not write a separate
report, manifest or assertions file.

## Results and the gate

Each target gets one result:

- **passed**: you observed the expected behaviour, and the linked evidence shows it.
- **failed**: what you observed contradicts the expected result, or a test assertion failed.
- **untested**: the check could not run, or the evidence is not enough to judge it. Say why.

**Missing evidence is never a pass.** Do not turn untested into passed because other targets passed.

How results map onto the factory's status words:

- A review with any `failed` target, or any critical `untested` one, is `VERDICT: CHANGES`.
- A builder with any `failed` target, or any critical `untested` one, writes `STATUS: BLOCKED`, or marks that criterion
  honestly as not met.
- The approver treats a criterion that is `failed` or `untested`, or has no evidence, as not met.

## Delivering evidence

Deliver evidence to whoever asked for it, and nowhere else: no `gh` comments, no uploads, no remote. `AGENTS.md` forbids
pushing or touching a remote.

## Final response

For every changed feature, say how it was proved. When a screenshot could not prove it, name the test that does, with
the exact command and its outcome. A screenshot is not a substitute for a behavioural test. Give the commit and branch
tested, the count of passed, failed and untested targets, the paths of the key evidence, and any critical failed or
untested target. Never claim a check ran, or a screenshot was taken, unless you inspected the result.
