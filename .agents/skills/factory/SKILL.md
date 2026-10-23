---
name: factory
description: Run the software factory. Use when the user types /factory <feature>, /factory next, /factory approve <job-id>, /factory rework <job-id> <note>, or /factory paid <job-id> <usd>. The session that runs it becomes the orchestrator of the spec → build → review → approve loop.
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
at once, one per Claude session. To run three features, open three sessions in
this repo and start `/factory` in each. Your session orchestrates only the job it
started or was handed.

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
<repo>/factory/backlog.md                      queue for /factory next
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
<repo>/../<name>-factory/<job-id>/             the job's worktree, on factory/<job-id>
```

The builder, `reviewer-ux` and `reviewer-ui` each create their own
`evidence/round-<N>/<agent>/` folder for the round they work on (for a rework
run, the job's last round plus one) and name its files in their report. It is
the one place an agent writes besides its one file; you never create it.

The dashboard shows the images in `evidence/round-<N>/<agent>/` on the job's card
and in its drawer, the latest round with images by default. Only image files
(`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) are listed, and each caption comes
from the file name, so name files for what they show (`02-filter-applied-passed.png`
reads "Filter applied passed"), as the `evidence-driven-testing` skill says.

The dashboard has four views, picked by the links under its title and kept in
the address's hash, so Back and Forward work and a poll never changes the view.
Every view has the header bar under those links: the client and sprint from
`engagement.json`, the Executive Financial Pulse (this month's spend at
list price against the monthly budget, leaving out jobs with no date or price
and saying how many), the Export Executive Briefing button and the Live
Event Stream link, also opened by the `m` key. `#board` (the default, and for an
empty or unknown hash) shows the
engagement strip (stage, and spend for all jobs, all time), the Executive
Overview Panel, Pass rate and the six lanes, each with its card count. `#git`
shows the strip and the branch graph. `#cost` shows the strip, the cost per
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
  `needs-human`. Worktree setup counts as `spec`.
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

## /factory <feature>

1. **Create the job.** `mkdir -p factory/jobs`, then pick the next 3-digit number
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
   exists and has a line `Story points: N`, directly under the title block,
   with N one of 1, 2, 3, 5, 8, 13. Copy N into `job.json` as `points`. If
   `spec.md` is missing, or the line is missing or N is not one of those values,
   stop and tell the user which; never pick a value yourself.
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
with the prompt `<paths> Final round: N.`
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
unless a step says otherwise.

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
   git status --porcelain -- . ':!factory/backlog.md'
   git diff --name-only main...factory/<job-id>
   git diff --name-only factory/<job-id>...main
   ```
   - **Dirty paths** are every path the first command prints: modified, staged,
     deleted and untracked files, and both names of a rename
     (`R  old -> new`). `factory/backlog.md` is ignored: every `/factory next`
     edits it, and job branches never touch it, so a dirty backlog cannot
     conflict with the merge.
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
   checkout and `main` may have moved meanwhile; if any path overlaps now, or
   the checkout is not on `main`, take step 2's exit. Then:
   ```bash
   git -C <worktree> merge-base --is-ancestor main HEAD
   ```
   If it fails, `main` moved: go back to step 3. Make at most 3 attempts at
   steps 3 to 5; if `main` has moved again after the third, release the lock and
   go to **Needs human**, saying `main` kept moving; the human runs
   `/factory approve <job-id>` again once it is quiet.
6. **Merge.** The main checkout may still hold the user's uncommitted files.
   Only read them: never stash, commit, reset, check out, clean or otherwise
   touch them, here or in any other step.
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

Set `stage: "needs-human"`, append a `stage_log` entry and a `stage_changed`
event, and refresh `commit`
to the branch tip, and `behind` and `base`. The branch
and its worktree stay for the human to inspect. Tell the user which job is waiting, give the worktree path, and quote
why it stopped: `decision.md`, the BLOCKED reason from `build.md`, or the reason
**Merge** gave.

## /factory next

Read `factory/backlog.md` and take the first `- [ ]` item. Claim it with one
Edit that replaces its exact line with `- [x] <item> → claimed`. If the Edit
fails because the line changed, another session claimed it: re-read the backlog
and try the next `- [ ]` item. If none is left, say so and stop.

Run `/factory <item>`, and once step 1 has given it a job id, replace `claimed`
on its backlog line with the job id.

## /factory approve <job-id>

The job must be in `needs-human`. Run **Merge** with that job id.

## /factory rework <job-id> <note>

The job must be in `needs-human`.

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

## Reporting

Keep the user posted with one short line per transition, for example
`003-rate-limiting: round 2, security CHANGES, ux/ui/code PASS → resuming builder`.
When the job ends, say whether it merged or needs a human, and why.
