Title: Owner attention panel: list every job waiting on the owner with why and the exact command to run, and flag stuck jobs
Size: 5
Rank: 20
Files: factory/dashboard.html, .agents/skills/factory/SKILL.md

Notes:
Today the owner has to open each job to learn it is waiting. Add one panel on the board that lists every job in needs-human, with the reason (from decision.md, build.md or the merge step) and the exact command to run next (/factory approve, /factory rework or /factory cancel). Also flag a running job as stuck when its events.jsonl has had no new line for 30 minutes. Reads only job.json, decision.md, build.md and events.jsonl; it changes no job. Every count is clickable and equal to its rows. Overlaps the approval alert icon job (047) in factory/dashboard.html, so run after it merges.

Grooming proposal:
