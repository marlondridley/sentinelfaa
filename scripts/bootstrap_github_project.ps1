[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Repo = "marlondridley/sentinelfaa",
    [string]$ProjectTitle = "NAS FeedOps Sentinel — Engineering & Operational Readiness",
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-GhJson {
    param([string[]]$Args)
    $raw = & gh @Args
    if ($LASTEXITCODE -ne 0) { throw "gh failed: $($Args -join ' ')" }
    if ([string]::IsNullOrWhiteSpace(($raw -join ""))) { return $null }
    return ($raw -join "`n" | ConvertFrom-Json)
}

function Invoke-GhText {
    param([string[]]$Args)
    $raw = & gh @Args
    if ($LASTEXITCODE -ne 0) { throw "gh failed: $($Args -join ' ')" }
    return ($raw -join "`n")
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) is required. Run: winget install GitHub.cli"
}

if (-not $Apply) {
    Write-Host "DRY RUN. No GitHub objects will be created. Re-run with -Apply to create them."
}

# -----------------------------------------------------------------------------
# Source history
# The script intentionally reads the repository rather than relying on a
# hand-maintained backlog. PRs are the primary source of completed intent;
# commits are used to catch work that was committed directly to main.
# -----------------------------------------------------------------------------
$prs = Invoke-GhJson @("pr", "list", "--repo", $Repo, "--state", "all", "--limit", "1000", "--json", "number,title,state,isDraft,mergedAt,headRefName,baseRefName,body,url")
$commits = Invoke-GhJson @("api", "repos/$Repo/commits", "--paginate", "--slurp")
$commitList = @()
foreach ($page in $commits) { $commitList += $page }

Write-Host "Repository: $Repo"
Write-Host "PRs found: $($prs.Count)"
Write-Host "Commit pages/items read: $($commitList.Count)"

# -----------------------------------------------------------------------------
# Milestones. These are capability checkpoints, not individual implementation
# tasks. The descriptions reflect the actual trajectory visible in the PRs.
# -----------------------------------------------------------------------------
$milestones = @(
    @{ title = "01 — Operational Picture & Trust"; description = "Turn FAA SWIM data into a current, deterministic answer to: can PCT trust the operational picture? Includes feed health, current status, bounded Watch Officer output, alert semantics, dashboard workflow and evidence drill-down." },
    @{ title = "02 — Human Decision Workflow"; description = "Make the human decision process the center of the console: one review per condition, triage, monitoring, escalation, evidence, audit trail and execution gates." },
    @{ title = "03 — Agent Governance & Evaluation"; description = "Make agent behavior measurable, traceable and governed: AGT categories, evaluation gates, decision quality, latency, cost, provenance, evidence binding and fail-closed controls." },
    @{ title = "04 — Security, Reliability & Evaluator Readiness"; description = "Harden the platform and prepare a defensible evaluator demonstration: zero-trust workload identity, secrets custody, CI guardrails, observability, governance mappings and reproducible scenarios." }
)

# -----------------------------------------------------------------------------
# Capability backlog. Each item is tied to the milestone that best describes
# the capability, rather than the implementation branch that happened to build
# it. Existing work is recorded as historical evidence; open PRs are surfaced.
# -----------------------------------------------------------------------------
$issues = @(
    @{ title="Define deterministic operational trust assessment"; milestone=0; labels=@("type:architecture","area:trust","demo"); priority="P0 Critical"; evidence="ADR + Test"; body="Define the deterministic rules that produce the current data-trust assessment for PCT and KDCA/KIAD/KBWI/KADW. Model output must not own posture, counts, affected airports or trust." },
    @{ title="Unify feed-health computation across console and agents"; milestone=0; labels=@("type:architecture","area:ingest","area:trust"); priority="P0 Critical"; evidence="Test"; body="Use one deterministic feed-health source for dashboard, Watch Officer, alerts and investigations. History evidence includes PR #40, #41 and monitoring PR #44." },
    @{ title="Operational overview answers what matters now"; milestone=0; labels=@("type:feature","area:ui","demo"); priority="P0 Critical"; evidence="Screenshot + Test"; body="Keep the dashboard centered on posture, trust, critical conditions, affected airports and next human action rather than raw telemetry. History evidence includes PR #25 and PR #49." },
    @{ title="Critical alert semantics remain deterministic and current"; milestone=0; labels=@("type:test","area:alerting","area:trust"); priority="P0 Critical"; evidence="Test"; body="Protect the rule that a current CRITICAL condition drives the operational status while stale/history conditions are separated. History evidence includes PR #25 and #40." },
    @{ title="Evidence drill-down from every major conclusion"; milestone=0; labels=@("type:feature","area:ui","area:trust"); priority="P1 High"; evidence="Screenshot + Trace"; body="A user should be able to move from a conclusion to the evidence, source feed, timestamp and validation result. History evidence includes PR #12 and #49." },
    @{ title="Bound Watch Officer context and narrative contract"; milestone=0; labels=@("type:architecture","area:agents","area:trust"); priority="P1 High"; evidence="Test"; body="Keep deterministic posture and trust outside the model; provide a bounded decision-shaped projection to the narrative layer. History evidence includes PR #19, #20 and #25." },
    @{ title="Finish dashboard KPI and responsive layout behavior"; milestone=0; labels=@("type:feature","area:ui","demo"); priority="P2 Normal"; evidence="Screenshot"; body="Preserve readable KPI labels, badges, numbers and small-window folding across console pages. History evidence includes PR #24, #49 and #54." },
    @{ title="Make alert history versus current state explicit"; milestone=0; labels=@("type:feature","area:alerting","area:ui"); priority="P1 High"; evidence="Test + Screenshot"; body="Older alerts and sensor artifacts must not appear as current operational conditions. Link records to the review or evidence behind them." },

    @{ title="One human review per operational condition"; milestone=1; labels=@("type:feature","area:hitl","demo"); priority="P0 Critical"; evidence="Test"; body="Continue the condition-level review model: repeated readings attach to the open review; quiet periods can open a new review. History evidence: PR #11." },
    @{ title="Make the review queue the single source of human work"; milestone=1; labels=@("type:architecture","area:hitl","area:ui"); priority="P0 Critical"; evidence="Test + Screenshot"; body="Dashboard, Review Queue, Alert Log and Watch Officer should count and link to the same review queue. History evidence: PR #49." },
    @{ title="Manager escalation is immediately visible"; milestone=1; labels=@("type:feature","area:hitl","demo"); priority="P0 Critical"; evidence="Screenshot"; body="Surface manager-required work separately from routine monitoring and allow a direct why/evidence drill-down." },
    @{ title="Batch similar HITL requests safely"; milestone=1; labels=@("type:feature","area:hitl","demo"); priority="P1 High"; evidence="Test + Screenshot"; body="Allow operators to select similar reviews and apply only safe batch actions. Approve must remain an individual decision unless explicitly governed." },
    @{ title="Execution gate remains human-controlled"; milestone=1; labels=@("type:architecture","area:governance","area:security"); priority="P0 Critical"; evidence="Test"; body="Agents never stage, approve or execute. Preserve role-checked decision gates and ensure no ungated writer is exported. History evidence: PR #4, #5, #37." },
    @{ title="Audit every human decision and destructive action"; milestone=1; labels=@("type:feature","area:governance"); priority="P1 High"; evidence="Trace"; body="Capture actor, action, time, evidence and result in append-only records. History evidence: PR #39 and #4." },
    @{ title="Reconcile and expire unstaged critical conditions"; milestone=1; labels=@("type:feature","area:governance","area:alerting"); priority="P1 High"; evidence="Test"; body="Ensure CRITICAL conditions cannot disappear because a downstream step failed. Preserve reconcile/EXPIRES behavior from the readiness work." },
    @{ title="Expose the reason behind every review recommendation"; milestone=1; labels=@("type:feature","area:hitl","area:ui"); priority="P1 High"; evidence="Screenshot"; body="A review should show trigger, evidence, rule/assessment, agent observations, conflicts and the decision the human is being asked to make." },

    @{ title="Standardize agent decision-quality evaluation"; milestone=2; labels=@("type:architecture","area:evaluation","area:agents","demo"); priority="P0 Critical"; evidence="Metric + Test"; body="Define decision quality independently of the model. The agent cannot grade itself. Use operator decision, evidence binding, gateway usage/cost and sealed records as evidence." },
    @{ title="Instrument agent latency end to end"; milestone=2; labels=@("type:feature","area:evaluation","area:agents"); priority="P1 High"; evidence="Metric + Trace"; body="Capture request, model and total latency consistently through OpenTelemetry and the evaluation layer." },
    @{ title="Instrument model usage and cost"; milestone=2; labels=@("type:feature","area:evaluation","area:agents"); priority="P1 High"; evidence="Metric"; body="Track provider/model, input/output usage and cost, including refused, circuit-open and budget-exhausted calls. History evidence: PR #36 and #35." },
    @{ title="Bind agent claims and evaluations to evidence"; milestone=2; labels=@("type:architecture","area:governance","area:evaluation"); priority="P0 Critical"; evidence="Trace + Test"; body="Every material claim should be traceable to evidence IDs and the model call/provenance record behind it. History evidence: PR #35 and #4." },
    @{ title="Prevent agent self-grading and metric manipulation"; milestone=2; labels=@("type:test","area:governance","area:evaluation"); priority="P0 Critical"; evidence="Test"; body="Regression tests must demonstrate that evaluation results come from independent evidence and deterministic gates, not model assertions." },
    @{ title="Use AGT-native/compatible/Sentinel-specific metric categories"; milestone=2; labels=@("type:architecture","area:evaluation","area:governance"); priority="P1 High"; evidence="ADR + Test"; body="Preserve the category model introduced in PR #7/#8 and make every metric source explicit." },
    @{ title="Expose agent health numbers with action and evidence"; milestone=2; labels=@("type:feature","area:ui","area:evaluation","demo"); priority="P1 High"; evidence="Screenshot + Metric"; body="Health rows must state what the number is, what action it implies, what data supports it and where the related investigation/review lives. History evidence: PR #49 and commit 860e609." },
    @{ title="Standardize provider-neutral evaluation gates"; milestone=2; labels=@("type:architecture","area:evaluation","area:agents"); priority="P1 High"; evidence="Test"; body="Use one evaluation/gating contract regardless of whether the configured gateway model changes. Current production path is intentionally one provider/model." },

    @{ title="Complete zero-trust workload identity and secret custody evidence"; milestone=3; labels=@("type:architecture","area:security","demo"); priority="P0 Critical"; evidence="Trace + Test"; body="Maintain SPIFFE identity, Vault Agent sidecars, mTLS, no application-held credentials and documented custody. History evidence: PR #13, #14, #46." },
    @{ title="Keep security and supply-chain guardrails blocking"; milestone=3; labels=@("type:test","area:security","area:platform"); priority="P0 Critical"; evidence="CI"; body="Preserve gitleaks history scanning, Semgrep, mypy, Trivy and compose/deployment smoke checks. History evidence: PR #43 and #48." },
    @{ title="Complete platform observability and alert-path self-monitoring"; milestone=3; labels=@("type:feature","area:platform","area:evaluation"); priority="P1 High"; evidence="Metric + Trace"; body="Prometheus should see dashboard, Temporal and alerting health; Alertmanager should expose failures in the alert path itself. History evidence: PR #44 and #47." },
    @{ title="Finish governance framework mappings"; milestone=3; labels=@("type:documentation","area:governance","demo"); priority="P1 High"; evidence="ADR"; body="Complete and review the open NIST AI RMF 1.0 and ISO/IEC 42001:2023 mapping in PR #53, alongside the agent register in PR #52." },
    @{ title="Create reproducible evaluator scenarios and final demo path"; milestone=3; labels=@("type:test","area:evaluation","demo"); priority="P0 Critical"; evidence="Demo + Test"; body="Create deterministic scenarios for healthy feeds, degradation, stale data, feed disagreement, agent failure and provider failure. Demonstrate data → trust → awareness → evidence → human decision → governed AI." },
    @{ title="Resolve or explicitly disposition current open governance/security PRs"; milestone=3; labels=@("type:architecture","area:governance","area:security"); priority="P0 Critical"; evidence="PR Review"; body="Current open work observed in repository history: PR #50 outbound leak check, #51 model-claim fencing, #52 agent register, #53 NIST/ISO mapping. Review, merge, revise or close each with a recorded decision." }
)

# -----------------------------------------------------------------------------
# Labels
# -----------------------------------------------------------------------------
$labels = @(
    @{name="type:feature"; color="1f6feb"; description="Capability or product behavior"},
    @{name="type:bug"; color="d73a4a"; description="Defect"},
    @{name="type:architecture"; color="8250df"; description="Architecture or design"},
    @{name="type:test"; color="0e8a16"; description="Verification or regression coverage"},
    @{name="type:tech-debt"; color="fbca04"; description="Maintainability or debt"},
    @{name="type:documentation"; color="0075ca"; description="Documentation or governance mapping"},
    @{name="area:ingest"; color="5319e7"; description="SWIM ingestion and normalization"},
    @{name="area:trust"; color="5319e7"; description="Data trust and operational posture"},
    @{name="area:alerting"; color="5319e7"; description="Alerts and alert delivery"},
    @{name="area:hitl"; color="5319e7"; description="Human-in-the-loop workflow"},
    @{name="area:ui"; color="5319e7"; description="Dashboard and operator experience"},
    @{name="area:agents"; color="5319e7"; description="Agent behavior and orchestration"},
    @{name="area:evaluation"; color="5319e7"; description="Evaluation and decision quality"},
    @{name="area:governance"; color="5319e7"; description="Governance and policy controls"},
    @{name="area:security"; color="5319e7"; description="Security and identity"},
    @{name="area:platform"; color="5319e7"; description="Infrastructure and observability"},
    @{name="demo"; color="0e8a16"; description="Important to evaluator/demo path"},
    @{name="blocked"; color="d73a4a"; description="Blocked by another item"}
)

if ($Apply) {
    foreach ($label in $labels) {
        try {
            & gh label create $label.name --repo $Repo --color $label.color --description $label.description --force | Out-Null
        } catch {
            Write-Warning "Could not create/update label $($label.name): $($_.Exception.Message)"
        }
    }

    foreach ($m in $milestones) {
        $existing = Invoke-GhJson @("api", "repos/$Repo/milestones?state=all&per_page=100") | Where-Object { $_.title -eq $m.title } | Select-Object -First 1
        if (-not $existing) {
            $payload = @{title=$m.title; description=$m.description} | ConvertTo-Json -Compress
            $created = Invoke-GhJson @("api", "repos/$Repo/milestones", "--method", "POST", "--input", "-")
        }
    }
}

# -----------------------------------------------------------------------------
# Project creation is done through gh project because the GitHub connector does
# not expose Project v2 mutations. The command is intentionally isolated here.
# -----------------------------------------------------------------------------
if ($Apply) {
    $projectJson = Invoke-GhText @("project", "list", "--owner", "@me", "--format", "json") | ConvertFrom-Json
    $project = $projectJson.projects | Where-Object { $_.title -eq $ProjectTitle } | Select-Object -First 1
    if (-not $project) {
        Write-Host "Creating Project: $ProjectTitle"
        & gh project create --owner "@me" --title $ProjectTitle --format json
        if ($LASTEXITCODE -ne 0) { throw "Unable to create GitHub Project" }
        $projectJson = Invoke-GhText @("project", "list", "--owner", "@me", "--format", "json") | ConvertFrom-Json
        $project = $projectJson.projects | Where-Object { $_.title -eq $ProjectTitle } | Select-Object -First 1
    }

    $projectNumber = [int]$project.number
    Write-Host "Project #$projectNumber created/found."

    # Project fields. Status is built in; these are the additional management fields.
    $fields = @(
        @{name="Priority"; type="SINGLE_SELECT"; options="P0 Critical,P1 High,P2 Normal,P3 Later"},
        @{name="Area"; type="SINGLE_SELECT"; options="Ingest,Trust,Alerting,HITL,UI,Agents,Governance,Security,Platform,Evaluation"},
        @{name="Phase"; type="SINGLE_SELECT"; options="Foundation,Operational Picture,Human Decision,Agent Governance,Evaluator Readiness"},
        @{name="Risk"; type="SINGLE_SELECT"; options="None,Low,Medium,High,Critical"},
        @{name="Evidence"; type="SINGLE_SELECT"; options="None,Test,Metric,Screenshot,ADR,Trace,Demo"},
        @{name="Demo"; type="SINGLE_SELECT"; options="No,Yes"}
    )
    foreach ($f in $fields) {
        try {
            $opts = $f.options -split ',' | ForEach-Object { @{name=$_} } | ConvertTo-Json -Compress
            & gh project field-create $projectNumber --owner "@me" --name $f.name --data-type $f.type --single-select-options $opts | Out-Null
        } catch {
            Write-Warning "Field $($f.name) may already exist or could not be created: $($_.Exception.Message)"
        }
    }

    # Create issues and add them to the project.
    foreach ($item in $issues) {
        $existingIssue = Invoke-GhJson @("issue", "list", "--repo", $Repo, "--state", "all", "--search", "in:title `"$($item.title)`"", "--limit", "20", "--json", "number,title") | Where-Object { $_.title -eq $item.title } | Select-Object -First 1
        if (-not $existingIssue) {
            $milestoneTitle = $milestones[$item.milestone].title
            $body = @"
## Capability
$($item.body)

## Project metadata
- Priority: $($item.priority)
- Evidence: $($item.evidence)
- Milestone: $milestoneTitle

## Repository history
This issue was derived from the repository's commits and pull requests, including the recent Sentinel work already merged into `main`. The bootstrap intentionally records capability-level work rather than duplicating every implementation commit.
"@
            $issue = Invoke-GhJson @("issue", "create", "--repo", $Repo, "--title", $item.title, "--body", $body, "--label", ($item.labels -join ','), "--milestone", $milestoneTitle, "--json", "number,url")
            & gh project item-add $projectNumber --owner "@me" --url $issue.url | Out-Null
            Write-Host "Created #$($issue.number): $($item.title)"
        } else {
            Write-Host "Exists: #$($existingIssue.number) $($item.title)"
        }
    }

    Write-Host "Project bootstrap complete: $ProjectTitle (#$projectNumber)"
} else {
    Write-Host "Would create $($milestones.Count) milestones, $($labels.Count) labels, $($issues.Count) capability issues, and one GitHub Project with management fields."
}

# -----------------------------------------------------------------------------
# Emit a local history report so the mapping remains inspectable.
# -----------------------------------------------------------------------------
$report = @"
# Sentinel GitHub Project Bootstrap Source Report

Repository: $Repo
Generated: $(Get-Date -Format o)

## Pull requests read
$($prs | ForEach-Object { "- #$($_.number) [$($_.state)] $($_.title)" } | Out-String)

## Recent commits read
$($commitList | Select-Object -First 100 | ForEach-Object { "- $($_.sha.Substring(0,7)) $($_.commit.message.Split("`n")[0])" } | Out-String)

## Milestones
$($milestones | ForEach-Object { "- $($_.title): $($_.description)" } | Out-String)

## Capability issues
$($issues | ForEach-Object { "- $($_.title) -> $($milestones[$_.milestone].title)" } | Out-String)
"@

$reportPath = Join-Path (Split-Path -Parent $PSCommandPath) "github-project-source-report.md"
Set-Content -Path $reportPath -Value $report -Encoding UTF8
Write-Host "History report written to $reportPath"
