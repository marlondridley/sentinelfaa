# Five-minute walkthrough

A scripted demonstration for someone who has five minutes and wants to know two things: what question this system answers, and why they should trust how it answers it. Each step says what to click and what to say.

## Before the audience arrives

None of this counts against the five minutes.

1. **Bring the stack up** from the repository root: `.\launch_nas_sentinel.ps1`. Add `-WithLiveFeed` if you have a SWIM subscription and want live FAA data on screen.
2. **Trust the local CA** once: import `nas-feedops-sentinel\tls\edge-ca.crt` into the Windows certificate store. The dashboard, the API and Keycloak (which serves HTTPS only) all present certificates it signed.
3. **Have an operator** with the `watch_officer` role: `.\nas-feedops-sentinel\scripts\Setup-Keycloak.ps1`, then sign in at https://localhost:8443. For the auditor contrast in step 5, create a second login with `$env:APP_ROLES = 'auditor'` before running the script again.
4. **Put something in the review queue.** Only real FAA data goes in; there is no synthetic mode. Live PCT data is usually quiet: the ten-message capture in the test fixtures raises 14 alerts, none of them CRITICAL. Run the live feed (`-WithLiveFeed`), or replay captured SWIM files:
   ```powershell
   cd nas-feedops-sentinel
   docker compose --env-file .env run --rm --no-deps sentinel python -m src.main --mode replay --path <folder>
   ```
   Replayed events are tagged `replay`, and every page will say so. That warning is part of the demonstration, not something to hide.

## The five minutes

### 0:00 · The question (Overview)

**Say:** "This watches the FAA's SWIM feeds for the four Potomac TRACON airports. It answers one question: can we trust the aircraft-movement data SWIM is publishing right now, and if not, who needs to look at what?"

**Show:**
- The **verdict** at the top: GREEN, AMBER or RED. It is computed by fixed rules from schema conformance, ingest lag and open CRITICAL alerts. No model is consulted, so the verdict can never disagree with the numbers under it.
- The **yellow banner**: "Not all data here is live FAA feed data". It counts the replayed events, if you replayed a capture. Every event is tagged with where it came from at ingest, and a file or API client cannot label itself live.
- The **AI investigation** card: five indicators for the latest investigation, no more. Evidence quality, confidence, investigation time, cost, and "Human review: Required". Every figure is measured from the record, not reported by the agents.
- The **Watch Officer brief** sits below the verdict and is visibly subordinate to it: a narrative, not a verdict.

### 0:45 · Isolated or sector-wide? (Feed Health)

Click the **schema conformance** or **lag** tile. Every number on the Overview is a link, and Feed Health opens on the view that number came from.

**Show:** the **Service by facility** matrix, one cell per SWIM service per airport.

**Say:** "A red column is a service problem. A red row is an airport problem. NO DATA is its own state: 'nothing arrived' and 'arrived and was fine' are different claims."

### 1:30 · What fired, and where (Alert Log)

Back on the Overview, click the **CRITICAL** tile. The Alert Log arrives pre-filtered.

**Show:**
- **Where the alerts cluster**: concentration in one rule points at one root cause.
- **Spread across facilities**.
- Select a row to read the full alert.

**Say:** "Seven deterministic rules produce every alert. Three are CRITICAL: a disconnected feed, runway visual range below 1,200 feet, and a degraded ISMC link. Every CRITICAL alert is staged for a human by rules, whether or not any model answers."

### 2:15 · The human decides (AI Critic & Review)

Open **AI Critic & Review** and go to the **Human review queue**.

**Show:**
- One staged action, labelled synthetic on its face, with the Watch Officer's **bottom line**.
- Expand **Investigation: evidence, challenges, what to verify**:
  - *Established by the evidence*, *Contested* and *Verify before deciding*. Every statement lists the evidence ids it cites.
  - The **Red-Team Critic**, whose job is the strongest contradicting explanation. Its rule-based challenges cannot be removed by the model; here it challenges the alert precisely because the data is not live.
  - The footer line: the record's SHA-256 and "integrity verified", checked against the stored record as the page renders.
- Expand **How this investigation was evaluated**: its quality, safety, cost, latency and audit gates, each with what decided it (an Agent Governance Toolkit component, or Sentinel's own check), and where the time and the money went.

**Say:** "Four specialists worked this: a feed diagnostician and a correlation analyst in isolation, then a red-team critic, then the Watch Officer's synthesis. They never talk to each other. They write to one structured record, and a claim that doesn't cite collected evidence is rejected before the record can be sealed. If the model is down, every specialist falls back to its rules and the step log says so."

**Do:** type a **decision note**, for example "verified against STARS console; contacting KDCA tower", and click **Approve escalation**.

### 3:30 · The trail proves it (Decision audit trail)

Scroll to the **Decision audit trail**.

**Show:**
- Two rows: `(new) → PENDING` by the system, then `PENDING → APPROVED` by you.
- Your row carries your name, your realm roles and how you signed in (`[keycloak-password]`).

**Say:** "The database refuses UPDATE and DELETE on this table, and the application's own database role can only read and insert. And the model had no path to that button: it explains, it doesn't decide."

If you prepared an auditor login: sign in as the auditor and open the same queue. The buttons are disabled, and the tooltip names the role they need.

### 4:15 · Boundaries, and the proof (close)

**Say:**
- "What it watches is SWIM, the FAA's published copy of this data. It can't see a controller's scope and doesn't claim to."
- "It holds data back when the FAA says to. Messages flagged `blockAtFAA` are withheld entirely. Beacon codes for flight plans that aren't active yet are never stored or shown. Positions and callsigns are stripped before anything reaches a model."
- "It never pages anyone or contacts an FAA system. 'Approve escalation' records a decision; it doesn't execute one."

**Show** the one-command proof, run earlier or live if time allows:

```bash
docker compose --profile proof run --rm proof
```

It walks the whole path against a throwaway database: the ten real captured messages, the injected fault, an auditor refused, a watch officer recorded, and the audit log refusing UPDATE and DELETE. CI runs it on every push.

## Likely questions

**Is this what controllers see?**
No. Controllers work from STARS. SWIM is the FAA's publication of that data to outside users, so Sentinel tells you whether the published picture is trustworthy. A gap in SWIM is a reason to investigate, not proof of what is on a scope.

**What happens when the model is down, slow, or wrong?**
Staging never depends on the model, so every CRITICAL alert is queued anyway. Each call has a 90-second timeout and a per-pass budget, and the rest run on rules. Every model output passes a schema guardrail; malformed output is recorded as a degradation, never stored as fact.

**Where does the data go?**
Every model call goes through one LiteLLM gateway, redacted first, under a scoped key with a budget and a rate limit. The provider is a line of gateway configuration. For real FAA operational data it would be an authorized endpoint such as Bedrock GovCloud, Azure OpenAI Government, or on-premises vLLM or NIM.

**How is the AI governed, and how do you know it is any good?**
Every model call and every tool an agent uses is decided first by a Microsoft Agent Governance Toolkit policy: deny by default, audited, and no agent may decide, write or stage, whatever credentials it presents. Quality is never self-reported. The **Agent Evaluation & Governance** page measures grounding, accuracy against operator decisions, missed escalations, calibration, latency, cost and reliability from the record, and gives one verdict that `python -m src.readiness --gate` turns into a deployment gate. [agent-governance.md](agent-governance.md) walks through the eight boundaries.

**How do you know the audit log wasn't edited?**
The database refuses UPDATE and DELETE on it, and the application role cannot even try. Investigation records are sealed with SHA-256 and re-verified on display. The log is not yet hash-chained or signed; that is on the known-limits list.

**What if the same message arrives twice?**
Event and alert identities come from the SWIM message itself, so a re-dropped file, a double scan or a retried workflow updates the same rows. It never produces a second alert or a second staged action.

**Would this scale to the full NAS?**
Not as built. The Jumpstart Kit writes one file per message, which suits one TRACON on one host. A production deployment would consume the Solace broker directly. The rules, staging, review and audit path behind the ingest step does not depend on how messages arrive.

## If something goes wrong

- **The review queue is empty.** Nothing real has raised a CRITICAL alert yet. Replay a capture as in setup step 4, or wait for the live feed.
- **The decision buttons are disabled.** Your login lacks `watch_officer`; the tooltip names the missing role. Grant it with `scripts/keycloak-rbac.sh` (`GRANT_USERS`, `GRANT_ROLES`); see [operations.md](operations.md).
- **The browser warns about the certificate.** Import `tls\edge-ca.crt` into the Windows certificate store.
- **Sign-in fails.** Its error message says which setting to fix. `scripts\Collect-Diagnostics.ps1` gathers container state and logs into one file without any secrets.
