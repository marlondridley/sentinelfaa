---
name: factory
description: Run the software factory. Use when the user types /factory <feature>, /factory next, /factory approve <job-id>, or /factory rework <job-id> <note>. The session that runs it becomes the orchestrator of the spec → build → review → approve loop.
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
<repo>/factory/jobs/<job-id>/job.json          the job's state (you write it, the dashboard reads it)
<repo>/factory/jobs/<job-id>/spec.md
<repo>/factory/jobs/<job-id>/build.md
<repo>/factory/jobs/<job-id>/rework-<k>.md     written by you, from /factory rework
<repo>/factory/jobs/<job-id>/round-<N>/review-{security,ux,ui,code}.md
<repo>/factory/jobs/<job-id>/decision.md
<repo>/factory/jobs/.merge-lock/holder         the merge lock; holder names the job that is merging
<repo>/../<name>-factory/<job-id>/             the job's worktree, on factory/<job-id>
```

### job.json

```json
{ "id": "003-rate-limiting", "feature": "Rate limit the signup form",
  "branch": "factory/003-rate-limiting",
  "worktree": "/Users/you/code/my-app-factory/003-rate-limiting",
  "stage": "review", "round": 2,
  "reviews": { "security": "CHANGES", "ux": "PASS", "ui": "pending", "code": "PASS" } }
```

- `stage` is one of `spec`, `build`, `review`, `approve`, `merged`,
  `needs-human`. Worktree setup counts as `spec`.
- `round` is the current review round (0 until the first round starts).
- `reviews` holds the current round only. Each value is `pending`, `PASS` or
  `CHANGES`. Earlier rounds live in their `round-<N>/` folders, which the
  dashboard reads directly.
- Rewrite the whole file on every change. `worktree` is always an absolute path.

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
   `round: 0` and all reviews `pending`.
2. **Worktree.** Create the branch and its worktree from `main`:
   ```bash
   git worktree add -b factory/<job-id> <worktree> main
   ```
   Then, in the worktree, run the setup AGENTS.md gives for a fresh checkout
   (installing dependencies, for example). If any of it fails, stop and tell
   the user.
3. **Spec.** Spawn `spec-writer` with the prompt
   `<paths> Feature: <feature, verbatim>`.
   Wait for it to finish, then check that `spec.md` exists.
4. **Build.** Set `stage: "build"`. Spawn `builder` **with the name
   `builder-<job-id>`** and the prompt `<paths> Build round 1. Port: <port>.`
   Wait for it to finish, then go to **Build check**.

### Build check

Read the first line of `build.md`.

- `STATUS: READY`: start the next review round.
- `STATUS: BLOCKED`: go to **Needs human**.

### Review round N

1. Set `stage: "review"`, `round: N` and all four reviews to `pending`. Create
   `factory/jobs/<job-id>/round-<N>/`.
2. Spawn all four reviewers **in one message**, so they run in parallel. Each
   gets the prompt `<paths> Review round N.`, plus its port for `reviewer-ux`
   and `reviewer-ui`.
3. **WAIT FOR ALL.** Each time a reviewer finishes, read the first line of its
   file (`VERDICT: PASS` or `VERDICT: CHANGES`) and write that verdict to
   `job.json` straight away, so the dashboard chips fill in as they land. The
   round is done only when all four `review-*.md` files exist in `round-<N>/`.
   Never resume the builder mid-round. If a reviewer finishes without writing
   its file, or writes a first line that isn't a verdict, stop and tell the user.
4. Decide:
   - **All PASS**: go to **Approve**.
   - **Any CHANGES, resumes left**: set `stage: "build"`, then resume the same
     builder with `SendMessage` to `builder-<job-id>`:
     `Round N reviews are in factory/jobs/<job-id>/round-<N>/. Address every review with VERDICT: CHANGES, commit, and update build.md for round N+1.`
     Wait for it to finish, then go to **Build check**.
   - **Any CHANGES, no resumes left**: write `decision.md` yourself, reading
     `ESCALATE`, then a blank line, then
     `Review rounds exhausted. Outstanding CHANGES in round-<N>/ from: <reviewers>.`
     Go to **Needs human**.

### Approve

Set `stage: "approve"`. Spawn `approver` with the prompt `<paths> Final round: N.`
When it finishes, read the first line of `decision.md`:

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
worktree not clean, main checkout busy, `main` kept moving. Remove only the lock
you took in this Merge; never delete or break another job's lock.

1. **Take the merge lock.**
   ```bash
   mkdir factory/jobs/.merge-lock && echo <job-id> > factory/jobs/.merge-lock/holder
   ```
   `mkdir` without `-p` is atomic: it fails if the folder exists, the same trick
   as claiming a job number. The leading dot keeps it out of job numbering, and
   `/factory/jobs/` already keeps it out of git. If `mkdir` fails, another merge
   is running: tell the user the job id in `factory/jobs/.merge-lock/holder` and
   that you are waiting, then try the `mkdir` again every 30 seconds.
   - If the holder is plainly dead (no session is merging it: for example it
     names this job, or its branch `factory/<holder>` no longer exists), tell
     the user it is a stale lock that they remove with
     `rm -r factory/jobs/.merge-lock`. Never remove it yourself. Keep trying the
     `mkdir`, so the merge goes on as soon as they have removed it.
   - After 30 minutes without the `.merge-lock`, set `stage: "needs-human"` and
     tell the user which job holds the lock. Once that merge has finished (or
     they have removed a stale lock), they run `/factory approve` with this
     job's id, not the holder's. You never held the lock, so there is nothing
     to release.
2. **Check the main checkout.** The merge happens there, so it must be clean and
   on `main`: `git status --porcelain -- . ':!factory/backlog.md'` prints
   nothing and `git branch --show-current` prints `main`. `factory/backlog.md`
   is exempt: every `/factory next` edits it, and job branches never touch it,
   so a dirty backlog cannot conflict with the merge. If not, release the lock,
   set `stage: "needs-human"` and tell the user the job is approved but the main
   checkout is busy; they run `/factory approve <job-id>` once it is clean.
3. **Bring the branch up to date.** The worktree must be clean
   (`git -C <worktree> status --porcelain` prints nothing), or the re-test would
   include edits that never merge; if not, release the lock and go to
   **Needs human**, saying the worktree has uncommitted changes. Then merge
   `main` into the job branch in its worktree, as a normal merge commit. Never
   rebase or force.
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
   check, since the user may have changed the main checkout meanwhile; if it is
   busy now, take step 2's exit. Then:
   ```bash
   git -C <worktree> merge-base --is-ancestor main HEAD
   ```
   If it fails, `main` moved: go back to step 3. Make at most 3 attempts at
   steps 3 to 5; if `main` has moved again after the third, release the lock and
   go to **Needs human**, saying `main` kept moving.
6. **Merge.**
   ```bash
   git merge --no-ff factory/<job-id> -m "Merge factory/<job-id>" &&
     git worktree remove <worktree> &&
     git branch -d factory/<job-id>
   ```
   Then release the lock and set `stage: "merged"`. If the merge fails, run
   `git merge --abort` (when a merge is in progress), then release the lock, set
   `stage: "needs-human"` and tell the user why. If removing the worktree fails,
   the job is still merged: release the lock, set `stage: "merged"` and tell the
   user what `git worktree remove` printed. Never push.

### Needs human

Set `stage: "needs-human"`. The branch and its worktree stay for the human to
inspect. Tell the user which job is waiting, give the worktree path, and quote
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
2. Set `stage: "build"`.
3. Spawn a fresh `builder` named `builder-<job-id>` with the prompt
   `<paths> Rework: read rework-<k>.md and address it, then update build.md for round N+1. Port: <port>.`
   (`N` is the job's last round.) A rework run gets a fresh `MAX_RESUMES`.
4. Continue from **Build check**, exactly like a new job.

## Reporting

Keep the user posted with one short line per transition, for example
`003-rate-limiting: round 2, security CHANGES, ux/ui/code PASS → resuming builder`.
When the job ends, say whether it merged or needs a human, and why.
