---
name: real-data-only
description: Keep synthetic, demo and sample data out of the live Sentinel stack and its operational database. Use when the dashboard is empty and you want to populate it, when asked to demo, show, seed or prove the system works, when loading, replaying or generating events, or when touching src/demo_scenario.py, src/sample_data.py, scripts/e2e_proof.py or anything that writes data_origin. Only real FAA SWIM data goes in, either replayed captures or the live feed.
compatibility: Applies to the live stack run by launch_nas_sentinel.ps1 and to nas-feedops-sentinel/src. Replay uses Docker Compose.
metadata:
  area: data
---

# Real data only

**Never put synthetic, demo or sample data into the live stack or its database.** The owner has explicitly ruled out demo data. To show the system working, replay real captured FAA SWIM data or run the live feed. Nothing else.

A quiet dashboard is not a bug. Live PCT data is usually quiet. Do not fix an empty or calm screen by injecting events.

## What exists in the code

- `python -m src.main` has `--mode replay` and `--mode eval` only. The generators that used to be run modes wrote synthetic events into the operational database; they were removed and now exist as test fixtures. Do not add a mode, flag, button or script that writes synthetic events to the operational database.
- `src/demo_scenario.py` (a cascade failure at KDCA) and `src/sample_data.py` produce events tagged `data_origin="synthetic"`. Tests and the end-to-end proof use them.
- `--mode eval` scores the agents on built-in scenarios and prints a report. `src/agent_evaluator.py` makes no storage calls. It does call the model, so it spends model budget.
- Every event and alert carries `data_origin`: `live` (the Jumpstart Kit's folder), `replay`, `synthetic`, `api` (posted to `/v1/ingest`), or `unknown` for rows from before the tag. Alerts inherit it, and are `mixed` when they span origins. A database constraint enforces the set, and a file or API client cannot label itself live.
- Non-live data is never silent. Every page warns while it is present, the Dashboard verdict counts live data only, the review queue labels non-live actions, and the Red-Team Critic challenges any alert not from the live feed.

## To put something on screen

1. **Is the live feed running?** If not, propose starting it: `.\launch_nas_sentinel.ps1 -WithLiveFeed` (the `stack-bring-up` skill covers the launcher). It opens a live subscription to the FAA broker and needs a SWIM subscription and Java 25, so get the owner's yes first. Do not start it on your own.
2. **Or replay real captures.** From `nas-feedops-sentinel/`:
   ```powershell
   docker compose --env-file .env run --rm --no-deps sentinel python -m src.main --mode replay --path <folder>
   ```
   `<folder>` is a path as the container sees it, so check the compose volumes before assuming a host path works. The Ingest and Replay page does the same from the console for a signed-in watch officer or supervisor. The real captures in the repo are under `nas-feedops-sentinel/tests/fixtures/swim_kit/` (the ten-message `capture_2026-08-27` raises alerts but none are CRITICAL).
3. **Expect the warnings, and leave them.** Replayed events are tagged `replay`, the yellow "Not all data here is live" banner appears, and the Dashboard verdict reads NO DATA while the live watcher is off. That is the system working as designed, not something to hide or work around.
4. **Do not fabricate a CRITICAL.** If a demonstration needs one, say that real data has none right now and ask the owner what to do. A synthetic review "labelled synthetic on its face" belongs in the disposable test database only.

## Where synthetic data is allowed

Only in a **disposable** database: unit tests, and `scripts/e2e_proof.py`, which refuses to run unless the database name contains a disposable marker (test, ci, scratch or tmp) and wipes it first. The operator identities in that proof are labelled synthetic.

Tests truncate tables. Never run tests, the proof or any generator with `POSTGRES_*` pointing at the live stack. The `ci-like-test-run` skill shows how to run them on a throwaway Postgres.

## Check before you write

- **Which database am I about to write to?** Read `POSTGRES_HOST` and `POSTGRES_DB`. If it could be the live one, stop.
- **Did the request say** demo, sample, populate, seed, fake or mock? Stop and offer a real replay or the live feed instead.
- **Is the data tagged right?** Anything you ingest must carry the honest `data_origin`. Never set `live` for a replay or an API post.

## Report

Say which origin is now on screen (live, replay, or both) and that nothing synthetic was written to the live database. If you could not populate the screen with real data, say that plainly rather than substituting something else.
