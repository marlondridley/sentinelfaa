# Project skills

Procedures for working on this repository, written as [Agent Skills](https://agentskills.io) (a folder with a
`SKILL.md`). The format is not tied to one product.

| Skill | Use it to |
|---|---|
| `stack-bring-up` | Start or redeploy the local Docker stack, and recover it after a Docker Desktop restart. |
| `ci-like-test-run` | Run lint, types and the test suite the way GitHub CI does, without touching the live stack. |

## Where each tool reads them

No single folder works for every tool, so the skills live in two places with identical content:

| Folder | Read by |
|---|---|
| `.agents/skills/` (**edit here**) | Codex, Gemini CLI, GitHub Copilot, Cursor, OpenCode |
| `.claude/skills/` (generated) | Claude Code, and also Copilot, Cursor, OpenCode |

Claude Code does not read `.agents/`, and Codex does not read `.claude/`. The `.claude/skills/` copy is real
files, not symlinks, because this repository is developed on Windows with `core.symlinks=false`.

After editing a skill here:

```
python scripts/sync_agent_skills.py          # refresh the .claude/skills/ copy
python scripts/sync_agent_skills.py --check  # exit 1 if the two copies differ
```

Tools that read both folders see each skill twice with the same content. They resolve that themselves and may log a
name-collision warning; it is harmless.

## Adding a skill

Create `.agents/skills/<name>/SKILL.md` with `name` (must equal the folder name; lowercase letters, digits and
hyphens) and `description` (what it does and when to use it, at most 1024 characters, no `: ` in unquoted YAML).
Keep to the base Agent Skills fields so every tool accepts it. Tool-specific fields (for example Claude Code's
`disable-model-invocation`) are ignored or rejected by other tools. Refer to bundled files relative to the skill
folder, never by a tool-specific path. Then run the sync script.
