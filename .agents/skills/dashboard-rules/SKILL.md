---
name: dashboard-rules
description: Rules for changing the Sentinel Streamlit dashboard so it stays quick and unambiguous for a watch officer. Use when adding or changing a dashboard page, tile, count, table, link, tooltip or layout, when a count and the list under it disagree, or when asked to restyle. Covers one job per page, counts that are clickable and equal their rows, tables that say who acts and what to do, no walls of text, and keeping the approved theme.
compatibility: Applies to nas-feedops-sentinel/dashboard (Streamlit) and src/review_queue.py. Tests run in Docker, see the ci-like-test-run skill.
metadata:
  area: dashboard
---

# Dashboard rules

The console exists so a watch officer can get from a signal to the exact item in one click, and so a system engineer can see what to fix. Moving targets, generic landing pages and walls of text cost them time and read as filler. A number that does not match the list beside it costs trust in the whole console. Earlier versions of the console failed on exactly these points and were reworked, so treat the rules below as requirements, not taste.

## The look is approved, so keep it

The theme (`dashboard/lib/theme.py`: dark Blueprint palette, KPI tiles, sections) is approved. Do not restyle it, and ask before any broad visual change, saying what would change. Small legibility fixes are fine when the owner asked for them.

`tests/test_dashboard_typography.py` holds a floor you must not lower: no declared font under 13px, secondary text at WCAG AA contrast on the dark panels, tooltips that are focusable and carry their text in the markup.

## The rules

1. **One job per page.** Operate: Dashboard (is the picture trustworthy, and what needs someone), Review Queue (decide), Alert Log (the record). Observe: Live Airspace, Feed Health. System: Ingest and Replay, Agent Evaluation. The map is in `dashboard/app.py`. If a new page or block looks like one that exists, it is the wrong design: link to the page that does the job, at the point of need. Open every page with `theme.page_header(title, question)` naming the one question it answers.
2. **Every count is clickable and equals the rows it opens.** Build tiles with `theme.kpi(..., href=theme.drill_href(page, **filters))`. The target page reads `st.query_params` and arrives already filtered. When a page lets the reader change a view that a link can also set, the choice is written back to the address and the address wins on arrival, so a count's link and the rows it opens cannot disagree. When you add a count, add a test that the number equals the rows after the click, in the style of `test_each_tile_counts_reviews_and_opens_the_rows_it_counts` in `tests/test_review_queue_page.py`.
3. **One unit per count, and the unit for work is the review.** A review is one per condition: a condition that fires two hundred times is one row. Counts of open reviews come from `src/review_queue.py` (`open_reviews`, `awaiting`, `monitored`, `escalations`). Never recount them in a page. A count in another unit, such as the Critic's readings, must name its unit and must not take the queue's place (see `test_the_critics_activity_counts_readings_and_is_not_the_queue`).
4. **Work that is done leaves the list.** A decided review leaves the queue (it stays viewable read-only through `review_queue.find`). "Continue monitoring" keeps the review open and still decidable, so it moves from awaiting to monitored. Say which one a row is in.
5. **Every link lands on its exact subject.** Deep-link with filters, so a review opens its detail, an airport opens its own rows, and a dimension opens with `?dimension=<name>`. No generic landings. No scrolling tickers or BREAKING/LIVE theatrics: the attention strip is a still list of at most five items (`dashboard/lib/attention.py`).
6. **Every table row answers "so what do I do, and who".**
   - Name the acting role (watch officer or system engineer) and the action.
   - Link to the review or ticket, and to the agent behind a number.
   - Use the reader's meaning of a column: "Source" is the data being processed, not who computed it.
   - Spell out tags and acronyms in words. An internal tag like `AGT-native` or `ECE` must not reach a page; `calculated_by` in `dashboard/lib/agent_eval.py` turns tags into words.
   - "Evidence" says what was counted, in the number's unit, never a bare number.
   The pattern to copy is the Agent Evaluation table: `policies/model-slos.yaml` carries `data`, `watch_officer` and `system_engineer` text per objective, and `src/evaluation_evidence.py` supplies "Behind this number".
7. **Explanations live in tooltips, not on the page.** Use `theme.help_hint(text)`. No multi-sentence paragraphs. The page shows data.
8. **Provenance stays visible.** Replayed, API and synthetic data is tagged `data_origin`, and pages warn when non-live data is present. The Dashboard verdict counts live data only. The review queue lists open reviews of every origin, because a replayed fault's review is still real work, and `OpenReview.live` says which count toward the verdict. A new number must state whether it includes non-live data. See the `real-data-only` skill.
9. **Text from outside is never trusted.** Anything from the database or a model that reaches `unsafe_allow_html` goes through `theme._esc`, and Markdown or CSV output goes through `dashboard/lib/safe_text.py`. `tests/test_dashboard_output_safety.py` holds this.

## KPI tile layout

Learned the hard way, so do not undo it:

- `.kpis` (the strip) is the size container and a grid. Each tile with a number is a subgrid spanning four rows (badge, label, number, footnote), so numbers line up across a strip. **Never put `container-type` on `.kpi`**: a tile cannot be both a size container and a subgrid.
- The strip decides the badge layout from its own width (`kpi_stack_width`, `KPI_BESIDE_MIN_PX`). Do not reintroduce a fixed pixel reservation for the "BREAKDOWN →" badge.
- A new tile whose label plus badge is wider than the limit needs `KPI_BESIDE_MIN_PX` raised. Measure it in a browser; the measured need for "CONFORMANCE" beside the badge was about 197px against a constant of 200.
- Six-tile strips fold to two rows of three below `KPI_FOLD_BELOW_PX` (1100). Five-tile strips were last known to spill below that width, and folding them was not approved. Check before relying on either fact.

## Before you call it done

- Run the dashboard tests inside the test image: `test_dashboard_page`, `test_review_queue_page`, `test_alert_log_page`, `test_agent_evaluation_page`, `test_dashboard_theme`, `test_dashboard_typography`, `test_dashboard_output_safety`. The `ci-like-test-run` skill shows how.
- For a layout change, look at it in a browser at about 1024, 1280, 1440 and 1920px wide. Check one number height per row, no badge over a label, and no spill.
- Preview against a throwaway database with real captures, never the live database and never demo data.

## Report

Say what changed on which page, which rule it touches, which tests ran, and what you saw at which widths. If you did not look in a browser, say so.
