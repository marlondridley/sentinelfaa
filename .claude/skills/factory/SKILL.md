---
name: factory
description: Run the software factory. Use when the user types /factory <feature>, /factory next, /factory idea <title>, /factory approve <job-id>, /factory rework <job-id> <note>, /factory paid <job-id> <usd>, /factory cancel <job-id> <why>, or /factory revive <job-id> <stage>. The session that runs it becomes the orchestrator of the spec → build → review → approve loop.
---

# Factory

You are the orchestrator for one job. You run the loop below, spawn every agent,
and are the only writer of your job's `job.json`. You never write code, specs,
reviews or decisions yourself. Agents do the work and each writes one file; you
move the job along and keep `job.json` current. If you cannot spawn the agents
in `.claude/agents/`, stop and tell the user.

## The loop

```
feature → spec-writer → builder → [security, ux, ui, code] → WAIT FOR ALL
                           ▲                                      │
                           └──── any CHANGES: resume builder ◄────┤
                                                                  ▼ all PASS
                                     merge to main ◄─ APPROVE ─ approver ─ ESCALATE → needs-human
```

| Step         | Agent (`subagent_type`) | Writes                                      |
| ------------ | ----------------------- | ------------------------------------------- |
| spec         | `spec-writer`           | `spec.md`                                   |
| build        | `builder`               | `build.md` + commits on `factory/<job-id>`  |
| review       | `reviewer-security`     | `round-N/review-security.md`                |
|              | `reviewer-ux`           | `round-N/review-ux.md`                      |
|              | `reviewer-ui`           | `round-N/review-ui.md`                      |
|              | `reviewer-code`         | `round-N/review-code.md`                    |
| approve      | `approver`              | `decision.md`                               |

`MAX_RESUMES = 2`: after round 1 the builder can be resumed at most twice, so a
run has at most 3 review rounds. To change the factory, edit this skill at
`.agents/skills/factory/SKILL.md` (the source) and then run
`python scripts/sync_agent_skills.py` to regenerate the `.claude/skills/` copy;
edit the agents in `.claude/agents/`.

Every agent reads `AGENTS.md` (or `CLAUDE.md`) for the repo's setup, dev server,
validation commands and conventions. Nothing repo-specific lives in these files.
The agents record what they tested by the `evidence-driven-testing` skill: proof
method and result per criterion in their one file, and screenshots in the job's
evidence folder (see **Files**).

## Parallel jobs

Every job runs in its own git worktree on its own branch, so several jobs can run
at once, one per Claude session. To run more than one feature, open one session
per feature in this repo and start `/factory` in each. Your session orchestrates
only the job it started or was handed.

`MAX_RUNNING_JOBS = 3`: at most this many running jobs at once, so parallel jobs
share the machine and Docker fairly. Change the number here and nowhere else.

- A job is running when its `job.json` `stage` is `spec`, `build`, `review` or
  `approve`. Jobs in `needs-human`, `merged` or `cancelled` are not running.
- To count running jobs, read every `factory/jobs/*/job.json` and go by `stage`
  alone. Skip folders whose name starts with `.` (such as `.merge-lock`, which
  is not a job).
- `/factory <feature>` (step 1) and `/factory next` count running jobs before they
  claim anything. At `MAX_RUNNING_JOBS` or more, they create and claim nothing
  and tell the user which jobs are running, with each job's id and stage, so the
  user can run the command again once one finishes. There is no queue and no
  waiting.
- `/factory approve`, `/factory rework` and `/factory paid` are not blocked by
  the cap: they act on a job that already exists, and approving or reworking one
  is how a slot frees up. `/factory cancel` is not blocked either: cancelling
  frees a slot. `/factory idea` is not blocked: an idea (a feature written
  down for later, with no job yet) costs no slot, the backlog has no limit, and
  only promotion, turning an idea into a job, takes a slot (see **The backlog**). `/factory revive` is blocked,
  because reviving claims a slot.
- Your session stays in the main checkout. Agents work in the job's worktree.
  The main checkout is the user's, and it may be dirty or on another branch.
  Only **Merge** touches it.
- Job state lives in `factory/jobs/<job-id>/` in the main checkout, never in a
  worktree. Each job has its own `job.json`, so sessions never write the same
  file.
- Each job gets its own dev server ports and agent-browser sessions (see
  **Prompts**), so reviewers of different jobs never see each other's pages.
- Merges are serialised by the merge lock, and every merge is re-tested against
  the current `main` before it lands (see **Merge**).

## Files

`<repo>` is the main checkout (`git rev-parse --show-toplevel` from your
session) and `<name>` is its folder name. `factory/jobs/` is gitignored runtime
state that never lands in a commit.

```
<repo>/factory/backlog/<slug>.md               one idea waiting in the backlog (committed; see The backlog)
<repo>/factory/backlog/done/<job-id>.md        a promoted idea, moved here by /factory next (committed)
<repo>/factory/prices.json                     list price per model, for the dashboard's full price (committed)
<repo>/factory/engagement.json                 optional, owner-written: client, stage, sprint, monthly_budget_usd, owner (the name shown in the pulse label) for the dashboard; never created or defaulted
<repo>/factory/jobs/<job-id>/job.json          the job's state (you write it, the dashboard reads it)
<repo>/factory/jobs/<job-id>/events.jsonl      the job's event log (you append to it, only the dashboard's live event stream reads it)
<repo>/factory/jobs/<job-id>/spec.md
<repo>/factory/jobs/<job-id>/build.md
<repo>/factory/jobs/<job-id>/rework-<k>.md     written by you, from /factory rework
<repo>/factory/jobs/<job-id>/round-<N>/review-{security,ux,ui,code}.md
<repo>/factory/jobs/<job-id>/decision.md
<repo>/factory/jobs/<job-id>/evidence/round-<N>/{builder,ux,ui}/   screenshots, made by those agents
<repo>/factory/jobs/.merge-lock/holder         the merge lock; holder names the job that is merging
<repo>/factory/cancelled/<job-id>.md           summary of a cancelled job, from /factory cancel (committed, one per cancelled job)
<repo>/../<name>-factory/<job-id>/             the job's worktree, on factory/<job-id>
```

The old checkbox list `factory/backlog.md` is retired: nothing reads or edits
it any more, and this skill leaves it untouched for the owner to remove.

The builder, `reviewer-ux` and `reviewer-ui` each create their own
`evidence/round-<N>/<agent>/` folder for the round they work on (for a rework
run, the job's last round plus one) and name its files in their report. It is
the one place an agent writes besides its one file; you never create it.

The dashboard shows the images in `evidence/round-<N>/<agent>/` on the job's card
and in its drawer, the latest round with images by default. Only image files
(`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) are listed, and each caption comes
from the file name, so name files for what they show (`02-filter-applied-passed.png`
reads "Filter applied passed"), as the `evidence-driven-testing` skill says.

The dashboard has four views, kept in the address's hash, so Back and Forward
work and a poll never changes the view. The links under its title pick three of
them (Board, Git Graph & Inspector, Cost & LOE); the fourth, `#stream`, is opened by the
header's Live Event Stream button or the `m` key.
Every view has the header bar under those links: the client and sprint from
`engagement.json`, the Executive Financial Pulse (this month's spend at
list price against the monthly budget, leaving out jobs with no date or price
and saying how many), the Export Executive Briefing button and the Live
Event Stream button, which opens the stream view, as does the `m` key. `#board` (the default, and for an
empty or unknown hash) shows the
engagement strip (stage, and spend for all jobs, all time), the Executive
Overview Panel (one row of five cards: the four overview tiles, then Pass
rate) and the six lanes, each with its card count: Backlog, Spec, Build,
Review & Audit, Client Approval, Merged. The Backlog lane holds the owner's
idea files from `factory/backlog/` (not `done/`), read-only, by rank, with
title, size and rank; then each job set to rework (stage `build` with one or
more `rework-<k>.md` files), outlined orange, until its stage changes; then each
cancelled job, outlined grey. Rework is a mark on a card, no longer a lane. `#git`
(Git Graph & Inspector) shows the strip, the branch graph as the job list and,
for the job picked in it (the newest at first), a stages bar (Spec, Build,
Review, Approver; a stage with no file yet is greyed out), its file tree and a
viewer. `#cost` shows the strip, the cost per
point tile and a table with one row per job: stage, points, full price and cost
per point. `#stream` shows the live event stream from each job's
`events.jsonl`.

The dashboard's job drawer has a compliance matrix, read from these same files.
Only the rows the factory really runs show a result: the four review verdicts of
the latest round (with each reviewer's earlier rounds), the approver's decision
and, on `ESCALATE`, the reason from the `## Why` section of `decision.md`. The
SAST, DAST, dependency vulnerability, credential leak (Vault), license,
copyleft (GPL), ISO 27001, SOC 2 and FedRAMP rows are placeholders that always
read `not run`, until a job builds that check. Status is only ever `passed`,
`failed` or `not run`.

The dashboard page has a `window.exportBriefing()` function that opens a
print-ready snapshot of the jobs merged in a chosen period, read from
`job.json`, `build.md` and `decision.md`, that the owner saves as PDF from the
browser's print dialog. The header bar's Export Executive Briefing button
calls it.

### job.json

```json
{ "id": "003-rate-limiting", "feature": "Rate limit the signup form",
  "branch": "factory/003-rate-limiting",
  "worktree": "/Users/you/code/my-app-factory/003-rate-limiting",
  "commit": "4f2a9c1", "behind": 3, "base": "9b07e3d",
  "stage": "review", "round": 2,
  "reviews": { "security": "CHANGES", "ux": "PASS", "ui": "pending", "code": "PASS" },
  "points": 3, "paid_usd": null, "merged_at": null,
  "stage_log": [
    { "stage": "spec", "at": "2026-10-21T13:02:11Z" },
    { "stage": "build", "at": "2026-10-21T13:06:40Z" },
    { "stage": "review", "at": "2026-10-21T13:31:05Z" },
    { "stage": "build", "at": "2026-10-21T13:52:18Z" },
    { "stage": "review", "at": "2026-10-21T14:10:44Z" }
  ],
  "usage": [
    { "agent": "spec-writer", "round": 0, "model": "opus", "tokens": 61234, "seconds": 212 },
    { "agent": "builder", "round": 1, "model": "opus", "tokens": 402118, "seconds": 1490 },
    { "agent": "reviewer-ux", "round": 1, "model": "sonnet", "tokens": null, "seconds": 318 }
  ] }
```

- `commit` is a short git id (`git rev-parse --short=7`). While the job is not
  merged it is the tip of `factory/<job-id>`
  (`git rev-parse --short=7 factory/<job-id>`, run from `<repo>`); once merged
  it is the merge commit on `main`. It is `null` until the branch exists.
  Refresh it every time you rewrite `job.json` after the branch changed: when
  the worktree is created, after each builder run (**Build check**), after
  **Merge** step 3 merges `main` into the branch, and whenever you set
  `stage: "needs-human"`. The page cannot run git and the merge deletes the
  branch, so this is the only git id the dashboard has for a job.
- `behind` is how far the branch has drifted from `main`: the number of commits
  on `main` the branch does not have
  (`git rev-list --count factory/<job-id>..main`, run from `<repo>`), an
  integer, `0` when the branch holds all of `main`. `base` is the short git id
  of the `main` commit the branch last synced to
  (`git rev-parse --short=7 $(git merge-base main factory/<job-id>)`, run from
  `<repo>`). Both are `null` until the branch exists, and `null` once merged,
  because the merge deletes the branch. Refresh both at exactly the points you
  refresh `commit`, except the final merge (**Merge** step 6), where you set
  both to `null` and `commit` to the merge commit. They count against local
  `main`, since branches are never pushed, and show the drift as of the last
  refresh; they never block a job.
- `stage` is one of `spec`, `build`, `review`, `approve`, `merged`,
  `needs-human`, `cancelled`. Worktree setup counts as `spec`. `cancelled` is
  an end state: only `/factory cancel` sets it, and only `/factory revive`
  leaves it.
- `round` is the current review round (0 until the first round starts).
- `reviews` holds the current round only. Each value is `pending`, `PASS` or
  `CHANGES`. Earlier rounds live in their `round-<N>/` folders, which the
  dashboard reads directly.
- `points` is the job's size in story points, copied from the `Story points: N`
  line of `spec.md` after the spec step (`null` until then). You never pick one.
- `paid_usd` is what the human actually paid for the job, in US dollars, set
  only by `/factory paid`. It stays `null` until they set it.
- `merged_at` is the UTC time you set `stage: "merged"`, as ISO 8601 with `Z`
  (`2026-10-21T14:05:00Z`), and `null` until then. The dashboard dates velocity
  by it.
- `stage_log` is the job's stage history, oldest first: one
  `{ "stage": "<stage>", "at": "<time>" }` entry for each time you set `stage`,
  with `at` the UTC time of that change from `date -u +%Y-%m-%dT%H:%M:%SZ`,
  taken at the moment you set it. Step 1 of **/factory <feature>** writes the
  first entry, `spec`. Every later time you set `stage` (each return to
  `build`, each `review`, `approve`, `needs-human` and `merged`), append one
  entry in the same rewrite, even when the stage is the one the job already
  had. Never edit, reorder or remove an entry. The `merged` entry has the same
  time as `merged_at`. The dashboard works out from it the time from spec to
  merge, what share of it was spent outside `needs-human` (flow efficiency),
  how long the owner kept a job waiting in `needs-human`, and the rework rate.
- `usage` is the log of agent runs, `[]` at first. Each time an agent run
  finishes, append one entry:
  `{ "agent": "<name>", "round": <N>, "model": "<model>", "tokens": <tokens>, "seconds": <seconds> }`.
  - `agent` is the `subagent_type` (`spec-writer`, `builder`,
    `reviewer-security`, `reviewer-ux`, `reviewer-ui`, `reviewer-code`,
    `approver`), and `model` is the `model:` value that agent's file in
    `.claude/agents/` sets.
  - `round` is `0` for the spec-writer, the round being built for a builder run
    (its first run and each resume), the review round for a reviewer, and the
    job's last round for a rework builder run and for the approver.
  - `tokens` is the total tokens the Agent result reports for that run, and
    `seconds` its duration, in whole seconds. If the result does not report a
    number, write `null`. Never estimate, carry over or invent a figure.
  - Time per round is the sum of `seconds` over the entries with that `round`;
    tokens per round likewise. Neither is stored: the dashboard adds them up,
    and works out the full price from `factory/prices.json` (tokens / 1e6 ×
    `usd_per_mtok[model]`, the list price with no plan discount). An entry with
    `null` tokens, or a model missing from `prices.json`, makes the full price
    read "unknown".
- Rewrite the whole file on every change. `worktree` is always an absolute path.
  Read the file's `paid_usd` before each rewrite and keep it, because
  `/factory paid` may have set it from another session. Carry `stage_log` over
  from the file the same way and append to it; never rebuild it.
- Jobs from before these fields existed have none of them; the dashboard shows
  their cost and timing figures and git id as `-`. Never back-fill them,
  `commit`, `behind`, `base` and `stage_log` included. A job whose `job.json`
  has no `stage_log` keeps none: never start one part-way through a job, and
  never work out times from file dates.

### Events

`events.jsonl` is your record of what you saw happen in the job, for the
dashboard's live event stream: one JSON object per line, appended with a single

```bash
printf '%s\n' '<json>' >> factory/jobs/<job-id>/events.jsonl
```

Never rewrite, edit, reorder or delete a line. Each line has `t`, the UTC time
from `date -u +%Y-%m-%dT%H:%M:%SZ` taken when the transition happens, and
`event`, one of:

```json
{"t":"2026-10-21T13:06:40Z","event":"agent_started","agent":"builder","round":1}
{"t":"2026-10-21T13:31:02Z","event":"agent_finished","agent":"builder","round":1,"verdict":"READY"}
{"t":"2026-10-21T13:31:05Z","event":"stage_changed","from":"build","to":"review"}
{"t":"2026-10-21T14:05:00Z","event":"merged","commit":"4f2a9c1"}
```

- `agent_started` and `agent_finished`: `agent` is the `subagent_type` and
  `round` means what it means in `usage`. `agent_finished` also has `verdict`:
  the first-line result you read (`PASS`, `CHANGES`, `READY`, `BLOCKED`,
  `APPROVE`, `ESCALATE`), or `null` for the spec-writer, which has none, and
  for an agent that finished without a readable first line.
- `stage_changed`: `from` and `to` are `stage` values; `from` is `null` for the
  job's first event. Append one every time you set `stage`, in the same moment
  as its `stage_log` entry.
- `merged`: `commit` is the merge commit id you write to `job.json`.
- The job id is the folder name and is not repeated in the line.

Append `agent_started` immediately before you spawn or resume an agent (each of
the four reviewers gets its own line) and `agent_finished` as soon as it
returns, in the same moment as its `usage` entry. The steps below name each
append point. The dashboard shows an agent as running while its start has no
finish and the job's `stage` is `spec`, `build`, `review` or `approve`.

The log is separate from `job.json` and from the `stage_log` and `merged_at`
timestamps: `job.json` stays the only source of every figure, stage and card on
the dashboard. The log is for visibility only and may be missing, so nothing
but the dashboard's live event stream reads it; no agent reads it and no figure
depends on it. Never write an event for something you did not do, never
estimate a time, and never back-fill old jobs: a job without the file has no
events. If an append fails, tell the user and carry on; the loop never depends
on the log. It is bounded by the loop itself (about 20 lines per round), so it
is never rotated.

### Prompts

Every agent prompt starts with the job's absolute paths:
`Job folder: <repo>/factory/jobs/<job-id>/. Worktree: <worktree>. Branch: factory/<job-id>.`
Below, `<paths>` stands for that sentence.

Agents that open pages also get a port. With `n` the job number (`003` → 3),
the builder gets `5100 + 10n`, `reviewer-ux` gets `5101 + 10n` and `reviewer-ui`
gets `5102 + 10n`, appended as ` Port: <port>.`

**Test servers and browsers.** Every agent that starts a test server or a
browser stops it, and checks it stopped, before it writes its one file; left
running, they lock the job folder and Windows cannot remove the worktree.
- Serve without changing folder: never `cd` into the worktree first, e.g.
  `python -m http.server <port> --bind 127.0.0.1 --directory <served folder>`.
  A Sentinel dashboard is AGENTS.md's `docker run`, container `<job-id>-dash`.
- No wrappers: no `nohup`, no `&`, no detached shell; use the Bash tool's own
  background run. Write the process id (from the check below), or the container
  name, to `factory/jobs/<job-id>/evidence/round-<N>/<agent>/server.pid`
  (`<agent>` is `builder`, `ux` or `ui`).
- Name the browser session `<job-id>-<agent>`; closing it with
  `agent-browser --session <name> close` also ends its hidden Chrome and helpers.
- Before the report: stop the server, close the session, run the check. The
  report says "server stopped, browser closed, ports free", or what still runs.

The check lists listeners on exactly the job's three ports, `<p1>`, `<p2>`,
`<p3>` (job 043: 5530, 5531, 5532), never a range, and the sessions whose names
start with `<job-id>-`; leave out a port that is 8000. Remove the container by
name first, and look up a listener's name before stopping it. Git Bash, then PowerShell:
```bash
docker rm -f "<job-id>-dash" 2>/dev/null || true
netstat -ano | grep -E ':(<p1>|<p2>|<p3>) .*LISTENING' || true   # last column: process id
tasklist //FI "PID eq <pid>"   # its name, before any taskkill
taskkill //PID <pid> //T //F
agent-browser session list | sed -n 's/^ *\(<job-id>-.*\)$/\1/p'
```
```powershell
docker rm -f "<job-id>-dash" 2>$null
Get-NetTCPConnection -State Listen -LocalPort <p1>,<p2>,<p3> -ErrorAction SilentlyContinue | ForEach-Object { Get-Process -Id $_.OwningProcess | Select-Object Id, ProcessName }
Stop-Process -Id <pid> -Force
agent-browser session list | Where-Object { $_.Trim() -like '<job-id>-*' }
```
Never stop a Docker process (`com.docker.*`, `wslrelay`, `vpnkit`, `docker*`): it
holds a container's published port, and stopping it restarts Docker Desktop and
the live stack. Report its name and process id instead.

**Cleanup**: at the end of a job's loop the orchestrator runs the commands above,
stops each listener that is not Docker's by its process id, closes each listed
session, and checks again that nothing is left. It matches only this job's three
ports and session names that start with `<job-id>-`: never the owner's dashboard
on 8000, another job's ports, or a name that merely contains the job id. If a
process will not stop, tell the user its name and process id and carry on; a
merge never fails or stops because of a leftover process.

## The backlog

The backlog is the owner's list of ideas. An idea is a feature written down for
later: it has no job id, no folder in `factory/jobs/` and no branch. It gets a
job id only when it is promoted: `/factory next` turns it into a job on the
board. The backlog has no length limit; only promotion is gated, by
`MAX_RUNNING_JOBS`.

Each idea is its own small plain-text file, `factory/backlog/<slug>.md`,
committed to git, with LF line endings. `<slug>` is 2-5 lowercase words joined
by `-` (only `a-z`, `0-9` and `-`, no job number), unique within
`factory/backlog/`: the file name matches `^[a-z0-9]+(-[a-z0-9]+){1,4}\.md$`.
To make a slug from a title, lowercase it, turn every character other than
`a-z` and `0-9` into a space, and join the first 4 words with `-`; a title of
one word gets `-idea` added. A file whose name breaks the rule is never
promoted: `/factory next` stops and asks the owner to rename it.

Write an idea file with the Write tool, never with `echo` or a heredoc, so a
title holding `$(...)`, backticks or a line break never reaches the shell. A
title with a line break is joined into one line with single spaces, so it
cannot forge a header or a `Grooming proposal:` line.
In shell commands, only a slug that follows the rule goes into a path or a
commit message, inside single quotes, with paths after `--`.

The file is exactly these lines, in this order; a header line is `Key: value`
on one line:

```
Title: <one line, plain words; this is the feature text /factory <feature> receives>
Size: <1, 2, 3 or unknown>
Rank: <whole number, lowest runs first>
Files: <comma-separated repo paths the idea expects to touch, or unknown>

Notes:
<free text, any number of lines, may be empty>

Grooming proposal:
<empty until the grooming agent writes here>
```

- `Title:` is what the job will build, in one line.
- `Size:` is the owner's rough estimate in story points. Only the spec-writer's
  `Story points` line is binding (see step 3 of **/factory <feature>**).
- `Rank:` is the idea's place in the queue: a whole number, and the lowest is
  promoted first; ties go to the file name in alphabetical order. Ranks need
  not be consecutive, so reordering is editing one `Rank:` line. A new idea
  gets the highest `Rank` among the ideas directly in `factory/backlog/` plus
  10, or 10 when there are none.
- `Files:` lists the paths the idea expects to touch, so overlapping jobs can
  be spotted later.
- The notes are everything between `Notes:` and `Grooming proposal:`.
- The line `Grooming proposal:` and everything after it belong to the grooming
  agent. Nothing in this skill reads or edits that section, and the owner's
  text never goes below it.

A promoted idea moves with `git mv` to `factory/backlog/done/<job-id>.md` and
gains one header line, `Job: <job-id>`, directly after `Files:`. It keeps all
its other text. That file is the only done record; `done/` is created by the
first promotion. Job branches never touch `factory/backlog/`: promotion is
committed on `main` before the job's worktree is made from it.

The owner adds an idea with `/factory idea <title>`, or by hand: write a file in
the format above in `factory/backlog/` and commit it. Either way, they may edit
`Size:`, `Rank:`, `Files:` and the notes by hand, and commit the edit:
`/factory next` will not promote an idea file with uncommitted changes.

## /factory <feature>

1. **Create the job.** `mkdir -p factory/jobs`, then count running jobs (see
   **Parallel jobs**). At `MAX_RUNNING_JOBS` or more, stop: create no folder,
   and tell the user that `MAX_RUNNING_JOBS` jobs are already running, with
   each job's id and stage, and that they approve, rework or cancel one of
   them, then run the command again. Otherwise pick the next 3-digit number
   after the highest numbered folder in `factory/jobs/` (`001` if there are
   none) and a 2–4 word kebab-case slug of the feature: `004-blog-reading-time`.
   Claim it with `mkdir factory/jobs/<job-id>` (no `-p`). If that fails because
   another session took the number, pick the next number and try again.
   The worktree path is `$(cd .. && pwd)/$(basename "$PWD")-factory/<job-id>`,
   run from `<repo>`. Write `job.json` with that `worktree`, `stage: "spec"`,
   `round: 0`, all reviews `pending`, `commit`, `behind`, `base`, `points`,
   `paid_usd` and `merged_at` `null`, `usage: []`, and a `stage_log` holding
   one entry, `{ "stage": "spec", "at": "<now>" }` (see **job.json**). Start
   `events.jsonl` with `stage_changed` from `null` to `spec` (see **Events**).
2. **Worktree.** Create the branch and its worktree from `main`:
   ```bash
   git worktree add -b factory/<job-id> <worktree> main
   ```
   Set `commit` to `git rev-parse --short=7 factory/<job-id>`, refresh
   `behind` and `base` (see **job.json**), and rewrite `job.json`. Then, in
   the worktree, run the setup AGENTS.md gives for a fresh checkout
   (installing dependencies, for example). If any of it fails, stop and tell
   the user.
3. **Spec.** Append `agent_started` for `spec-writer`, round 0, then spawn
   `spec-writer` with the prompt `<paths> Feature: <feature, verbatim>`.
   Wait for it to finish, append its `usage` entry and `agent_finished` with
   `verdict: null`, then check that `spec.md`
   exists and has a line `Story points: N`, directly under the title block.
   A job of 1, 2 or 3 points is built; a job of 5, 8 or 13 is split, or is
   built as one job only when its spec says why it cannot be split. The
   spec-writer's size is binding; an idea's `Size:` is only an estimate. Never
   pick or change a value yourself.
   - N is 1, 2 or 3: copy N into `job.json` as `points` and go to step 4.
   - N is 5, 8 or 13: copy N into `job.json` as `points`, then look at
     `spec.md`:
     - It has a `## Split` section: follow **Too big: split into ideas**
       below. Do not build. This holds even if it also has the two sections
       below.
     - It has no `## Split` section, but has both a non-empty
       `## Dependencies` section and a non-empty
       `## Why this cannot be split` section: the spec-writer judged the
       dependencies not understood well enough to cut safe pieces. Keep
       `points`, write no ideas, cancel nothing, and go to step 4. Judging
       whether the two sections are good enough is the approver's job.
     - It has neither (or only one of the two sections, or an empty one):
       stop, change nothing, and tell the owner the spec-writer left no usable
       split and no stated reason why it cannot be split: they cancel the job
       or send it back with a note.
   - `spec.md` is missing, the line is missing, or N is any other value: stop
     and tell the user which.

   **Too big: split into ideas.** For a spec of 5 or more points the
   spec-writer writes a `## Split` section: pieces of 3 points or fewer that
   together cover the feature. (Its other outcome, the two sections above, is
   not split.) Each piece is a block of a `### <title>` line, then the
   lines `Size: <1, 2 or 3>`, `Files: <comma-separated paths or unknown>` and
   `Notes: <one or two plain sentences>`. You only copy its pieces into the
   backlog; you never promote, start, split or re-size a piece yourself.
   1. **Check the split.** `spec.md` must have a `## Split` section with at
      least two pieces, each with its `Size:`, `Files:` and `Notes:` lines. If
      not, stop, change nothing, and tell the owner the spec-writer left no
      usable split and no stated reason why it cannot be split: they cancel
      the job or send it back with a note. If a piece's `Size:` is not 1, 2 or
      3, write no ideas, cancel nothing, and tell the owner which piece.
   2. **Check the main checkout**, before writing anything. Make each piece's
      slug from its title (see **The backlog**). If the main checkout is not
      on `main`, stop, change nothing, and tell the owner to switch it back to
      `main` and then tell you to carry on. If a slug already exists in
      `factory/backlog/`, or two pieces give the same slug, stop, change
      nothing, and name the slug: the owner renames or removes the existing
      idea file, or edits the piece title in `spec.md`, then tells you to
      carry on.
   3. **Write one idea per piece**, in order, exactly as **/factory idea**
      does: `Title:` the piece's title, `Size:` and `Files:` from the piece,
      the notes from its `Notes:` line, an empty `Grooming proposal:` section,
      and `Rank:` after the existing ideas, so each piece is 10 higher than the
      one before. Commit each one alone, with its one path, as
      **/factory idea** does. The running-job
      cap does not apply: ideas take no slot. If a commit fails partway, stop:
      do not cancel the job. Tell the owner which ideas are committed, which
      one is written but not committed (they commit that one path or delete
      the file), and which were not written, and that nothing was started.
   4. **Cancel the job.** Run
      `/factory cancel <job-id> too big, split into <count> ideas`, with
      `<count>` the number of ideas written. The ideas are written first, so a
      failure here leaves them and the spec in place.
   5. **Report**, in plain words: each idea created (its path, title and size,
      the size the spec-writer gave), that the job was cancelled as too big,
      and that nothing was started. `/factory next` takes the idea with the
      lowest `Rank:` first, so the pieces run in the order listed unless the
      owner edits `Rank:` to change it; they run `/factory next` when ready.
4. **Build.** Set `stage: "build"` and append a `stage_log` entry and a
   `stage_changed` event. Append `agent_started` for `builder`, round 1, then
   spawn `builder` **with the name
   `builder-<job-id>`** and the prompt `<paths> Build round 1. Port: <port>.`
   Wait for it to finish, then go to **Build check**.

### Build check

Every builder run (first run, resume or rework) ends here. Append its `usage`
entry, refresh `commit` to the branch tip and `behind` and `base` with it, then
read the first line of `build.md` and append `agent_finished` for `builder`
with that `verdict` (`READY`, `BLOCKED`, or `null` if it is neither).

- `STATUS: READY`: start the next review round.
- `STATUS: BLOCKED`: go to **Needs human**.

### Review round N

1. Set `stage: "review"` and append a `stage_log` entry and a `stage_changed`
   event, and set `round: N` and all four reviews to `pending`. Create
   `factory/jobs/<job-id>/round-<N>/`.
2. Append one `agent_started` line, round N, for each of the four reviewers,
   then spawn all four **in one message**, so they run in parallel. Each
   gets the prompt `<paths> Review round N.`, plus its port for `reviewer-ux`
   and `reviewer-ui`.
3. **WAIT FOR ALL.** Each time a reviewer finishes, read the first line of its
   file (`VERDICT: PASS` or `VERDICT: CHANGES`) and write that verdict and the
   reviewer's `usage` entry to `job.json` straight away, so the dashboard chips
   fill in as they land, and append its `agent_finished` with that `verdict`.
   The round is done only when all four `review-*.md` files exist in
   `round-<N>/`.
   Never resume the builder mid-round. If a reviewer finishes without writing
   its file, or writes a first line that isn't a verdict, append its
   `agent_finished` with `verdict: null`, then stop and tell the user.
4. Decide:
   - **All PASS**: go to **Approve**.
   - **Any CHANGES, resumes left**: set `stage: "build"` and append a
     `stage_log` entry and a `stage_changed` event, append `agent_started` for
     `builder`, round N+1, then resume the same builder with `SendMessage` to
     `builder-<job-id>`:
     `Round N reviews are in factory/jobs/<job-id>/round-<N>/. Address every review with VERDICT: CHANGES, commit, and update build.md for round N+1.`
     Wait for it to finish, then go to **Build check**.
   - **Any CHANGES, no resumes left**: write `decision.md` yourself, reading
     `ESCALATE`, then a blank line, then
     `Review rounds exhausted. Outstanding CHANGES in round-<N>/ from: <reviewers>.`
     Go to **Needs human**.

### Approve

Set `stage: "approve"` and append a `stage_log` entry and a `stage_changed`
event. Append `agent_started` for `approver`, round N, then spawn `approver`
with the prompt `<paths> Final round: N.` When the job's `points` is 5 or more,
add to the prompt: `This job is <points> points and was not split: in
spec.md, read the ## Dependencies and ## Why this cannot be split sections,
and ESCALATE if either is thin (a reason of only "it is big", or dependencies
that do not show why the pieces cannot be built apart), or if Final round is
greater than 1.`
When it finishes, append its `usage` entry, then read the first line of
`decision.md` and append `agent_finished` for `approver` with that `verdict`
(`APPROVE`, `ESCALATE`, or `null` if it is neither):

- `APPROVE`: go to **Merge**.
- `ESCALATE`: go to **Needs human**.

### Merge

The job's reviews and tests ran against the `main` it was branched from, and
other jobs may have merged since. So Merge takes a lock, brings the branch up to
date with `main`, re-tests the result and only then merges. The lock is held for
the whole of Merge, re-test included, so a second session's merge waits until
the first has finished and then sees the new `main`. Run commands from `<repo>`
unless a step says otherwise. Merge never runs on a `cancelled` job, and
`/factory cancel` refuses a job while `factory/jobs/.merge-lock/holder` names
it (see **/factory cancel**).

**Every exit releases the lock.** Once you hold the lock, every path out of
Merge releases it with `rm -r factory/jobs/.merge-lock` before you set the stage
and tell the user: merged, merge failed, tests failed, conflict with `main`,
worktree not clean, main checkout has uncommitted changes to files this job
also changes (or is not on `main`), `main` kept moving. Remove only the lock
you took in this Merge; never delete or break another job's lock.

1. **Take the merge lock.**
   ```bash
   mkdir factory/jobs/.merge-lock && echo <job-id> > factory/jobs/.merge-lock/holder
   ```
   `mkdir` without `-p` is atomic: it fails if the folder exists, the same trick
   as claiming a job number. The leading dot keeps it out of job numbering, and
   `/factory/jobs/` already keeps it out of git. If `mkdir` fails, another merge
   is running: tell the user the job id in `factory/jobs/.merge-lock/holder` and
   that you are waiting, then try the `mkdir` again every 30 seconds. If
   `holder` is empty or missing, another session has only just taken the lock:
   keep waiting.
   - If the holder names this job, an earlier Merge of this job probably died
     and left a stale lock. Tell the user so: if no other session is merging
     this job, they remove it with `rm -r factory/jobs/.merge-lock`. Never
     remove it yourself, and do not call any other holder stale. Keep trying
     the `mkdir`, so the merge goes on as soon as they have removed it.
   - If you still cannot take `.merge-lock` after 30 minutes of waiting, set
     `stage: "needs-human"`, append a `stage_log` entry and a `stage_changed`
     event, and tell the user which job holds the lock. If that
     job's session is no longer running, the lock is stale and they remove it
     with `rm -r factory/jobs/.merge-lock`. Once that merge has finished (or the
     stale lock is gone), they run `/factory approve` with this job's id, not
     the holder's. You never held the lock, so there is nothing to release.
2. **Check the main checkout.** The merge happens there, so it must be on
   `main`, and none of its uncommitted files may be one the merge writes. Other
   uncommitted files are the user's work in progress and do not block the merge.
   `git branch --show-current` must print `main`. Then list the two sets of
   paths:
   ```bash
   git status --porcelain -- . ':!factory/backlog'
   git diff --name-only main...factory/<job-id>
   git diff --name-only factory/<job-id>...main
   ```
   - **Dirty paths** are every path the first command prints: modified, staged,
     deleted and untracked files, and both names of a rename
     (`R  old -> new`). The `factory/backlog/` folder is ignored: owners add
     and edit idea files by hand, job branches never touch that folder, and
     promotion commits only its own paths, so a dirty idea file cannot
     conflict with a merge.
   - **Job paths** are what the other two print. The first gives the files the
     job branch changes, counted from where it left `main` (three dots, so it
     is right both before and after step 3 merges `main` in). The second gives
     the files `main` changed since then: the final merge rewrites those too,
     so a dirty copy of one would be overwritten or block the merge.

   A dirty path overlaps when it is also a job path; an untracked file counts
   when the job adds a file at the same path. A dirty folder (`?? dir/`)
   overlaps when any job path is under it. If any path overlaps, or the
   checkout is not on `main`, release the lock, set `stage: "needs-human"`,
   append a `stage_log` entry and a `stage_changed` event, and tell the user
   the job is approved but cannot
   merge: the main checkout has
   uncommitted changes to files this job also changes (name each overlapping
   file), or is on another branch (name it). They commit, stash or remove those
   files, or switch back to `main`, then run `/factory approve <job-id>`.
3. **Bring the branch up to date.** The worktree must be clean
   (`git -C <worktree> status --porcelain` prints nothing), or the re-test would
   include edits that never merge; if not, release the lock and go to
   **Needs human**, saying the worktree has uncommitted changes; the human
   commits or removes them, then runs `/factory approve <job-id>`. Otherwise,
   merge `main` into the job branch in its worktree, as a normal merge commit.
   Never rebase or force.
   ```bash
   git -C <worktree> merge --no-edit main
   ```
   - If it conflicts, note the conflicting files git lists, then run
     `git -C <worktree> merge --abort`, release the lock and go to
     **Needs human**, saying the branch conflicts with `main` and naming those
     files. The human either sends the job back with
     `/factory rework <job-id> <note>` (for example "merge main and resolve the
     conflicts"), or merges `main` in the worktree by hand, resolves, commits
     and runs the AGENTS.md tests there, then runs `/factory approve <job-id>`:
     Merge will find the branch up to date and not re-test it.
   - If it fails any other way, release the lock and go to **Needs human**,
     quoting what git printed; the human fixes the cause and runs
     `/factory approve <job-id>`.
   - If it merged, refresh `commit` to the new branch tip, and `behind` (now
     `0`) and `base` with it.
   - If it prints `Already up to date.`, the branch already holds the current
     `main` and was tested as it is: skip step 4.
4. **Re-test the merged result.** `main` has moved, so tell the user you are
   re-testing against the current `main`, then run the validation commands
   AGENTS.md gives for build, lint and tests (and `infra` where AGENTS.md says
   to) in the worktree, with the job id as the run name (`WT_NAME`), and finish
   with AGENTS.md's `down` step whether they pass or not. Run long steps in the
   background, wait for them, and read the pytest summary line, not just the
   exit code. If anything fails, release the lock, go to **Needs human** and
   quote the failing output. Never merge on a failure. The human either sends
   the job back with `/factory rework <job-id> <note>`, or fixes the cause and
   runs the tests themselves before `/factory approve <job-id>`: the branch now
   holds `main`, so Merge will find it up to date and not re-test it.
5. **Confirm `main` did not move** while the tests ran. First repeat the step 2
   check, running its commands again, since the user may have changed the main
   checkout and `main` may have moved meanwhile:
   ```bash
   git branch --show-current
   git status --porcelain -- . ':!factory/backlog'
   git diff --name-only main...factory/<job-id>
   git diff --name-only factory/<job-id>...main
   ```
   If any path overlaps now, or the checkout is not on `main`, take step 2's
   exit. Then:
   ```bash
   git -C <worktree> merge-base --is-ancestor main HEAD
   ```
   If it fails, `main` moved: go back to step 3. Make at most 3 attempts at
   steps 3 to 5; if `main` has moved again after the third, release the lock and
   go to **Needs human**, saying `main` kept moving; the human runs
   `/factory approve <job-id>` again once it is quiet.
6. **Merge.** The main checkout may still hold the user's uncommitted files.
   Only read them: never stash, commit, reset, check out, clean or otherwise
   touch them, here or in any other step. Run **Cleanup** (see **Prompts**)
   first, so no leftover process holds the worktree.
   ```bash
   git merge --no-ff factory/<job-id> -m "Merge factory/<job-id>" &&
     git rev-parse --short=7 HEAD &&
     git worktree remove <worktree> &&
     git branch -d factory/<job-id>
   ```
   The id `git rev-parse` prints, right after the merge and before the worktree
   is removed, is the merge commit on `main`. Then release the lock and set
   `stage: "merged"`, `commit` to that id, `behind` and `base` to
   `null`, and `merged_at` to the current UTC time
   (`date -u +%Y-%m-%dT%H:%M:%SZ`), and append a `stage_log` entry for
   `merged` with that same time. In the same moment append a `merged` event
   with that `commit` and a `stage_changed` event to `merged`, both with that
   same time. If the merge fails, run
   `git merge --abort` (when a merge is in progress), then release the lock, set
   `stage: "needs-human"`, append a `stage_log` entry and a `stage_changed`
   event, and tell the user why;
   once they have fixed the cause they run `/factory approve <job-id>`. If
   removing the worktree fails, the job is still merged: release the lock, set
   `stage: "merged"`, `commit`, `behind`, `base` and `merged_at` as above,
   append the `merged` `stage_log` entry and the `merged` and `stage_changed`
   events as above, and tell the user
   what `git worktree remove` printed, and that `factory/<job-id>` is left to
   delete with `git branch -d` once the worktree is gone. Never push.

### Needs human

Run **Cleanup** (see **Prompts**), then set `stage: "needs-human"`, append a
`stage_log` entry and a `stage_changed` event, and refresh `commit`
to the branch tip, and `behind` and `base`. The branch
and its worktree stay for the human to inspect. Tell the user which job is waiting, give the worktree path, and quote
why it stopped: `decision.md`, the BLOCKED reason from `build.md`, or the reason
**Merge** gave.

## /factory next

Promotes the next idea in the backlog (see **The backlog**) to a job and runs
it. Run commands from `<repo>`.

1. **Cap.** Count running jobs (see **Parallel jobs**). At `MAX_RUNNING_JOBS`
   or more, stop and change nothing. Tell the user that `MAX_RUNNING_JOBS`
   jobs are already running, with each job's id and stage, and that they
   approve, rework or cancel one of them, then run `/factory next` again.
2. **Pick.** List the files directly in `factory/backlog/` (not `done/`).
   Every one must be named by the slug rule,
   `^[a-z0-9]+(-[a-z0-9]+){1,4}\.md$` (see **The backlog**); if any is not,
   stop before claiming anything and tell the owner, in plain words, which
   file to rename (2-5 lowercase words joined by `-`, ending `.md`) and to
   commit the rename, then run `/factory next` again. Otherwise read the
   `Rank:` line of each and take the idea with the lowest `Rank`; on a tie,
   the file name that comes first alphabetically. `<slug>` is its file name
   without `.md`. If there is none, say the backlog is empty and stop.
3. **Check the main checkout.** Promotion commits on `main` there, so
   `git branch --show-current` must print `main`, and
   `git status --porcelain -- 'factory/backlog/<slug>.md'` must print nothing. If
   either fails, stop before claiming anything and tell the owner which: switch
   the main checkout back to `main`, or commit or discard their edit to that
   idea file. Then they run `/factory next` again.
4. **Job id.** Choose the id exactly as step 1 of **/factory <feature>** does,
   with the idea's `Title` (plus its notes) as the feature text: the next
   number, a slug, and the claim `mkdir factory/jobs/<job-id>` (no `-p`).
   Write nothing in the folder yet.
5. **Claim the idea** by moving it:
   ```bash
   mkdir -p factory/backlog/done
   git mv -- 'factory/backlog/<slug>.md' 'factory/backlog/done/<job-id>.md'
   ```
   If `git mv` fails because the file is gone, another session promoted it:
   remove the folder you just made (`rmdir factory/jobs/<job-id>`, still
   empty) and go back to step 2 for the next idea by rank. If none is left,
   say so and stop. If it fails any other way, remove the folder too, then
   stop and quote what git printed.
6. **Commit the promotion.** Add the line `Job: <job-id>` directly after the
   `Files:` line of the moved file (with the Edit tool), then commit only the
   two paths, so the owner's other uncommitted files are never staged or
   touched:
   ```bash
   git add -- 'factory/backlog/done/<job-id>.md'
   git commit -m 'Promote <slug> to <job-id>' -- 'factory/backlog/<slug>.md' 'factory/backlog/done/<job-id>.md'
   ```
   The old path needs no `git add`: `git mv` already staged its removal, and
   `git add` on it fails. If `git commit` fails (for example a leftover
   `index.lock`), try again up to three times, a few seconds apart. If it
   still fails, never leave the file half-moved: put it back as it was
   committed and remove the job folder:
   ```bash
   git mv -- 'factory/backlog/done/<job-id>.md' 'factory/backlog/<slug>.md'
   git restore --source=HEAD --staged --worktree -- 'factory/backlog/<slug>.md'
   rmdir factory/jobs/<job-id>
   ```
   Then stop and tell the owner what git printed, that the idea is back where
   it was and nothing was started, and that they run `/factory next` again
   once the cause is fixed.
7. **Run it.** Carry on as `/factory <feature>`, with the feature text the
   idea's `Title`, followed by its notes when they are not empty, and the job
   id already made: finish step 1 from writing `job.json` (no new count, number
   or `mkdir`), then steps 2 onwards. The worktree is made from `main`, which
   now holds the move.

## /factory idea <title>

Adds one idea to the backlog (see **The backlog**). It is not blocked by
`MAX_RUNNING_JOBS`: an idea takes no slot. Run commands from `<repo>`.

1. **Make the slug** from the title by the rule in **The backlog** (the first
   4 words, lowercased, joined by `-`).
2. **Refuse**, changing nothing, when:
   - the main checkout is not on `main` (`git branch --show-current`): tell
     the owner to switch it back to `main`, then run the command again;
   - `factory/backlog/<slug>.md` already exists: name it, and tell the owner
     to start the title with different words, or edit the existing idea file
     instead.
3. **Rank** is the highest `Rank:` among the files directly in
   `factory/backlog/` plus 10, or 10 when there are none.
4. **Write** `factory/backlog/<slug>.md` with the Write tool (never `echo` or
   a heredoc), LF line endings, exactly:
   ```
   Title: <title>
   Size: unknown
   Rank: <rank>
   Files: unknown

   Notes:

   Grooming proposal:
   ```
5. **Commit** only that path:
   ```bash
   git add -- 'factory/backlog/<slug>.md'
   git commit -m 'Add idea <slug>' -- 'factory/backlog/<slug>.md'
   ```
   If the commit fails, tell the owner what git printed: the file is written
   but not committed, and they commit that one path.
6. **Tell the owner**, in plain words: the file's path; that they can edit
   `Size:`, `Files:` and the notes by hand (and `Rank:` to reorder); and that
   it has no job id yet: `/factory next` turns it into a job when it has the
   lowest `Rank:` and fewer than `MAX_RUNNING_JOBS` jobs are running.

## /factory approve <job-id>

The job must be in `needs-human`. Run **Merge** with that job id. A
`cancelled` job is refused, with nothing changed: the only way back for it is
`/factory revive`.

## /factory rework <job-id> <note>

The job must be in `needs-human`. A `cancelled` job is refused, with nothing
changed: the only way back for it is `/factory revive`.

1. Write the note, verbatim, to `factory/jobs/<job-id>/rework-<k>.md`, where `k`
   is the next unused number.
2. Set `stage: "build"` and append a `stage_log` entry and a `stage_changed`
   event.
3. Append `agent_started` for `builder`, round N, then spawn a fresh `builder`
   named `builder-<job-id>` with the prompt
   `<paths> Rework: read rework-<k>.md and address it, then update build.md for round N+1. Port: <port>.`
   (`N` is the job's last round.) A rework run gets a fresh `MAX_RESUMES`.
4. Continue from **Build check**, exactly like a new job.

## /factory paid <job-id> <usd>

Records what the human actually paid for the job: their own figure, for example
the metered cost on their billing page, or `0` on a flat plan. The factory never
fetches a bill. It works in any stage, `merged` included, because the bill
arrives after the job.

1. `factory/jobs/<job-id>/job.json` must exist, and `<usd>` must be a plain
   number of dollars, 0 or more (`12.40`, `0`). If either is wrong, change
   nothing and tell the user which.
2. Read that `job.json`, set `paid_usd` to the number, and rewrite that file.
   Touch no other job and no other field. This is the one command that may
   write a job another session runs; that session keeps `paid_usd` when it
   next rewrites the file (see **job.json**).
3. Tell the user the job id and the amount recorded.

## /factory cancel <job-id> <why>

Stops a job for good, unless the owner later revives it. It works from any
stage except `merged`. Refuse, with nothing changed, and tell the user why, when
`factory/jobs/<job-id>/job.json` does not exist, the job is `merged` (its work
is already on `main`), it is already `cancelled`, or
`factory/jobs/.merge-lock/holder` names it (Merge is running for it; cancel
again once it has finished). It is not blocked by `MAX_RUNNING_JOBS`. If another
session orchestrates the job, that session stops its loop when it sees the
stage is `cancelled`. If `<why>` is missing, ask the user for it once; never
write an empty `Reason:`. Otherwise, in this order:

1. **Stop its agents.** Stop the `builder-<job-id>` agent and any reviewer or
   approver still running for the job. For each one stopped, append
   `agent_finished` with `verdict: null`, and a `usage` entry only if the
   stopped run's result reports figures; if it reports none, write `null`
   tokens and seconds. Never estimate.
2. **Refresh git ids.** If the branch `factory/<job-id>` exists, refresh
   `commit`, `behind` and `base` from it, as in **Needs human**. Then run
   **Cleanup** (see **Prompts**); the worktree stays, but leftovers still go.
3. **Set the stage.** Set `stage: "cancelled"`, append a `stage_log` entry (only
   if the job already has a `stage_log`; never start one) and a
   `stage_changed` event from the previous stage to `cancelled`.
4. **Write the summary.** Write `factory/cancelled/<job-id>.md` in the main
   checkout (`mkdir -p factory/cancelled` first) in the format below, and
   commit it on `main` with that one explicit path and a one-line message, as
   AGENTS.md says for commits. If the main checkout is not on `main`, or
   already has uncommitted changes to that exact path, commit nothing: leave
   the file and tell the owner to commit `factory/cancelled/<job-id>.md`
   themselves once the main checkout is back on `main` with that path clean.
   The stage change still stands.
5. **Keep the rest.** Never delete the job folder, the branch or the worktree;
   the owner may revive the job. Never write `decision.md`: the reason lives in
   the summary file.

Tell the user the job id, the stage it was cancelled from and the summary
file's path, then, in plain words: its spend so far stays counted in the
monthly and all-time figures; it no longer takes one of the
`MAX_RUNNING_JOBS` running slots; nothing was deleted; and
`/factory revive <job-id> spec` or `/factory revive <job-id> build` brings it
back.

The summary file is plain text, exactly these ten labelled lines, one per line,
in this order:

```
Job: <job-id>
Feature: <feature from job.json>
Points: <points>
Reason: <why, verbatim>
Cancelled at: <time>
Cancelled from stage: <the stage before cancelled>
Branch: <branch>
Last commit: <commit>
Tokens: <total>
List-price spend (USD): <spend>
```

- `Reason` is `<why>` word for word, on one line: a reason given over several
  lines is joined with single spaces. The file is committed and the repository
  is public, so never put secrets or personal data in it; if `<why>` holds
  any, ask the user for a reason without them.

- `Points`, `Branch` and `Last commit` are the value, or `none` when it is
  `null` or no branch exists.
- `Cancelled at` is the UTC time from `date -u +%Y-%m-%dT%H:%M:%SZ`. For a
  migrated job where only the day is known, it is the date `YYYY-MM-DD` alone.
- `Tokens` is the sum of `tokens` over `usage`, and `List-price spend (USD)`
  the sum of tokens / 1e6 × `usd_per_mtok[model]` from `factory/prices.json`,
  to two decimals. If any entry has `null` tokens or a model missing from
  `prices.json`, both read `unknown`: never a partial sum, never an estimate.
  With no `usage` entries they read `0` and `0.00`.

A cancelled job's `usage` stays counted in the dashboard's monthly pulse, the
strip's all-time spend and the cost view: the tokens were spent. The dashboard
shows a cancelled job as a dimmed row in the Cost & LOE table, with a "spent on
cancelled work" line above it, and in the job drawer that row opens, with the
cancel summary and the revive command. It is not on the board, not in the
branch graph and not in any flow, cycle, rework, value or pass-rate figure.

**Migration.** Jobs `020-executive-briefing` and `027-pulse-in-header` were
cancelled before this stage existed: the first line of their `decision.md`
reads `CANCELLED`, while their `job.json` still says `build` and
`needs-human`. Their summary files, `factory/cancelled/020-executive-briefing.md`
and `factory/cancelled/027-pulse-in-header.md`, were written from their
`job.json` and `decision.md`, with `Cancelled at: 2026-10-23` and
`Cancelled from stage:` `build` for 020 and `spec` for 027, which never got a
spec written. Then only `stage` was set to
`cancelled` in each `job.json`: no `stage_log` entry, no event, no other field,
and their `decision.md` files stay as they are. `decision.md` no longer decides
whether a job is running; `stage` does.

## /factory revive <job-id> <stage>

Brings a cancelled job back. Otherwise change nothing and tell the user, in one
short sentence:

- the job is not cancelled: say its stage, and that only a cancelled job can be
  revived;
- `<stage>` is not `spec` or `build`: say those are the two stages a job can
  go back to.

1. Count running jobs (see **Parallel jobs**). At `MAX_RUNNING_JOBS` or more,
   change nothing and tell the user there is no free slot, which jobs are
   running, with each job's id and stage, and to approve, rework or cancel one
   of them first: reviving claims a slot.
2. Set `stage` to `<stage>`, and append a `stage_log` entry (only if the job
   already has a `stage_log`; never start one) and a `stage_changed` event
   from `cancelled`. Keep `usage`, `points`, `paid_usd` and every other field
   as they are, so the job's cost keeps adding up.
3. Leave `factory/cancelled/<job-id>.md` in place, unedited, as history.
4. Tell the user the job id, the stage it went back to, that its earlier spend
   carries on adding up, and that its summary file stays as history.
5. Carry on from that stage's step of **/factory <feature>**, without setting
   the stage again. If the worktree is gone, recreate it first: from the
   existing branch with `git worktree add <worktree> factory/<job-id>` (no
   `-b`), or by step 2 if the branch does not exist either. For `spec`, carry
   on at step 3. For `build`, it is step 4, with the builder building round
   `N+1` (`N` the job's last round, as for a rework run) and a fresh
   `MAX_RESUMES`. Your session now orchestrates the job.

## Reporting

Keep the user posted with one short line per transition, for example
`003-rate-limiting: round 2, security CHANGES, ux/ui/code PASS → resuming builder`.
When the job ends, say whether it merged or needs a human, and why.
