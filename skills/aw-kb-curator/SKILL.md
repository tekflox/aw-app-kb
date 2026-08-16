---
name: aw-kb-curator
description: Curate this workspace's knowledge base, agent memory, and skill health — review KB docs for staleness/duplicates/gaps, audit auto-memory entries for conflicts and drift, and flag skills with low retro scores or no recent use. Use when the user says "revisa o KB", "curate knowledge base", "kb-curator", "revisa a memória" or when running a scheduled knowledge audit.
---

# aw-kb-curator — Knowledge Base, Memory & Skill Health Curator

Three audits in one run:

1. **KB Audit** — this workspace's `kb` app content for staleness, duplicates, gaps
2. **Memory Audit** — Claude Code auto-memory entries for drift, conflicts, gaps (if this CLI has one)
3. **Skill Health** — installed `contributes.skills` docs vs. how much they're actually used

Run every audit that has something to check — some workspaces won't have all three wired up (see each part's "Skip if" note). Produce a unified report at the end.

---

## Part 1 — KB Audit

The `kb` app (Tier-2 container) owns its content inside its own volume — this
agent has **no direct filesystem path** to it. Everything goes through the
app's own REST routes (mounted at `/api/apps/kb/...` behind this workspace's
local identity guard) or its MCP tools.

### Listing files

```bash
curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:9030/api/apps/kb/api/kb/files
```

(Or, from inside a browser-authenticated session, `GET /api/apps/kb/api/kb/files`
directly — it needs whatever auth this workspace's `/api/apps/*` routes
already require.) Returns a flat JSON list of `{path, name, size, modified}`.
Excludes `mapped_folders/` results are still present — filter those out
client-side; they're auto-generated from a repo's `--map-path` run, not
curatable prose.

Each file's own content carries YAML frontmatter (`source`, `checksum`, an
edited timestamp) written by `update_knowledge_base` — read the file (`GET
/api/apps/kb/api/kb/file/{path}`, or `search_knowledge_base` if it's already
indexed) to get those.

### Curation workflow

**Step 1 — Inventory pass (metadata only)**

Read frontmatter of each file. Flag:

| Flag | Criterion |
|---|---|
| `stale` | Edited timestamp older than 60 days |
| `orphan-skill` | Under a `skills/` path but no matching entry in `GET /api/apps/-/skills` (Part 3) |
| `duplicate` | Two files with very similar titles or paths |
| `gap` | Topic referenced in other docs but no dedicated doc exists |

**Step 2 — Content read (flagged files only)**

For each flagged file decide:
- **Update** — outdated content → `update_knowledge_base`
- **Delete** — stale/orphaned/superseded → `delete_knowledge_base`
- **Merge** — near-duplicate → keep better one, delete other
- **Create** — gap → draft new doc via `update_knowledge_base`

**Step 3 — Skill docs cross-check**

Compare any `skills/<name>.md` KB entries against the live, actually-installed
skills index (Part 3's source of truth):

```bash
curl -s -H "X-AW-Local-Cli-Token: $(cat /opt/aw-workspace/.aw-workspace/cli-token)" \
  http://127.0.0.1:9030/api/apps/-/skills
```

A KB doc describing a skill with no matching live entry → mark orphan to delete.

---

## Part 2 — Memory Audit

**Skip if**: this CLI session has no `~/.claude/projects/<this-project>/memory/`
directory at all — not every agent runtime backing this workspace has
Claude Code's auto-memory feature wired up.

### Finding the memory dir

Don't hardcode a path — it's keyed by *this specific project's* filesystem
path (a slugified version of it), which differs per workspace/session:

```bash
ls -d ~/.claude/projects/*/memory/MEMORY.md 2>/dev/null
```

If nothing matches, this workspace/session has no auto-memory — skip this
part and say so in the final report rather than fabricating findings.

### Workflow

**Step 1 — Read the index**

```bash
cat <memory-dir>/MEMORY.md
```

Each line points to a memory file.

**Step 2 — Read all memory files**

Read every `.md` file listed in `MEMORY.md`. Memory files have frontmatter
with `type` (`user`, `feedback`, `project`, `reference`) and `description`.

**Step 3 — Flag issues**

| Flag | Criterion |
|---|---|
| `stale` | `project` type memory referencing a date/event that has clearly passed, OR referencing code constructs that may have changed (verify against current codebase) |
| `conflict` | Two memories that give contradictory guidance on the same topic |
| `outdated-fact` | Memory claims X exists/works a certain way — verify by reading the current code/file |
| `gap` | Important behavioral pattern or preference observed in recent sessions but not captured |

**Step 4 — Act**

- **Update stale memory**: edit the file with corrected content using the Write tool.
- **Delete obsolete memory**: remove the file AND remove its line from `MEMORY.md`.
- **Resolve conflict**: merge into one authoritative entry, delete the other.
- **Create new memory**: write a new `.md` file and add it to `MEMORY.md`.

Always keep `MEMORY.md` in sync — every add/delete must update the index.

Memory types and format: see the frontmatter convention in existing files
(`type`, `name`, `description` + body with **Why:** and **How to apply:**
for `feedback`/`project` types).

---

## Part 3 — Skill Health Review

### Live skills, source of truth

```bash
curl -s -H "X-AW-Local-Cli-Token: $(cat /opt/aw-workspace/.aw-workspace/cli-token)" \
  http://127.0.0.1:9030/api/apps/-/skills
```

Returns every `contributes.skills` entry across installed apps: `app`, `id`,
`description`, `skill_md_path`, `registered`. `registered: false` means the
app's declared skill never actually made it into `/opt/aw-workspace/skills/`
(a bad manifest path, an id collision with another app, or a framework bug)
— flag it immediately, that's a bug to fix, not a content problem.

For registered entries, `skill_md_path` points at
`/opt/aw-workspace/skills/<id>/SKILL.md` (top-level, unprefixed — the same
place this repo's own built-in skills live, where Claude Code auto-discovers
`SKILL.md` files from). Read it directly.

### Cross-check against agents-platform, if installed

**Skip if** no `aw__agents_platform_runners__*` MCP tools are available in this session
(the `agents-platform-runners` app isn't installed/configured here).

```
aw__agents_platform_runners__list_retro_scores
aw__agents_platform_runners__list_agents
```

For each live skill:
1. Is there a corresponding agent in agents-platform? If not, **unregistered**.
2. Does the agent have recent runs (`list_target_runs`)? None in 30 days → **unused**.
3. Retro score below 0.5 → **low-quality**.

### Skill doc freshness

For each `skill_md_path`, compare its mtime against the matching agent's
`updated_at` (if one exists). If they diverge significantly, the skill doc
may be **out-of-sync** with the agent's system prompt.

### Actions

| Flag | Action |
|---|---|
| `unregistered-in-index` (`registered: false`) | File a bug — this is a framework/manifest defect, not a content issue |
| `low-quality` | Read the SKILL.md + recent run lessons → propose specific improvements |
| `unused` | Determine if the skill is obsolete or just not triggered — propose deprecation or trigger condition update |
| `unregistered` (no matching agent) | Propose creating the matching agent in agents-platform, if that's the intended runtime |
| `out-of-sync` | Read both SKILL.md and agent system_prompt → propose which is the source of truth and update the other |

---

## Part 4 — KB Reachability Audit

> **Goal**: confirm that KB entries and skills being created are actually
> reachable by agents at runtime, not just that the doc exists somewhere.

### 4.1 — Which agents/bots carry the KB search instruction

**a) This workspace's own AGENTS.md/CLAUDE.md**, if it declares one — check
whatever root-level instructions file the CLI running this session loaded
(look for it before assuming a path; it may not exist in every workspace):

```bash
find / -maxdepth 2 -iname "AGENTS.md" -o -iname "CLAUDE.md" 2>/dev/null
grep -n "search_knowledge_base\|KB" <that file> | head -20
```

**b) agents-platform agents**, if installed (see Part 3's skip condition):

```
aw__agents_platform_runners__list_agents
```

Check each agent's `system_prompt` for `search_knowledge_base`.

**c) Installed skills** — cross-reference against the live skills index from Part 3:

```bash
for f in /opt/aw-workspace/skills/*/SKILL.md; do
  grep -lq "search_knowledge_base" "$f" || echo "MISSING KB: $f"
done

# Dead tool names. A skill naming one teaches an agent a lookup that fails,
# and an agent that concludes a tool is missing usually stops instead of
# asking. `aw-knowledge-base` and `agent-mcp` are pre-gateway spellings.
grep -rln "aw-knowledge-base\|mcp__agent-mcp__\|agents_platform__<tool>" \
     /opt/aw-workspace/skills/*/SKILL.md
```

Flag any agent/skill whose instructions **do not** mention the KB, and any
that still name a dead tool namespace.

### 4.2 — Analyze agent run histories for missed skills

**Skip if** no agents-platform MCP tools are available (Part 3's condition).

Look for sessions where a skill-relevant query was made but the skill was
never loaded. Signals:

- Agent improvised instructions instead of following a known SKILL.md
- User had to re-explain something already documented in a skill
- A skill's trigger keywords appeared in the conversation but the skill file
  was never read

```
aw__agents_platform_runners__list_target_runs(target_slug=<slug>, limit=10)
aw__agents_platform_runners__peek_run_output(run_id=<id>)
```

### 4.3 — Verify KB entries are positioned for discoverability

For each new/recently-edited KB entry (from Part 1):

1. Search for it: `search_knowledge_base(query=<topic>)` — does it appear in top-3?
2. If not, check if the path/title is too generic or the content lacks keywords
3. Rewrite the KB entry with better trigger vocabulary: include the exact
   phrases agents would search for

### 4.4 — `search_skills`/`load_skill` mount (fixed 2026-08-13, v0.18.0)

The `kb` app's own `search_skills`/`load_skill` MCP tools read from
`KB_SKILLS_DIR` (default `/app/skills`) **inside the container**. This used
to be unmounted — no volume in `aw-app-kb/aw-app.json` pointed it at the
workspace's real `/opt/aw-workspace/skills/`, so those two tools couldn't
see any live skill regardless of where it was registered. Fixed in
`aw-app-kb` v0.18.0 (`$AW_WORKSPACE_SKILLS` → `/app/skills`, ro) — confirm
the mount is still present in the manifest before relying on this being
current; if a future release drops it, re-flag as a regression using the
same wording this section used to carry.

### 4.5 — Actions

| Finding | Action |
|---|---|
| Agent/skill missing KB instruction | Propose adding `search_knowledge_base` guidance — flag for the workspace owner's approval before editing anything shared |
| Missed skill in run history | Add a KB entry under `memory/<skill-name>-trigger-patterns.md` mapping the user's natural-language phrasing to the skill |
| KB entry not discoverable | Rewrite via `update_knowledge_base` with richer keyword coverage |
| `search_skills`/`load_skill` unmounted again | Check the manifest for the `$AW_WORKSPACE_SKILLS` volume from 4.4; if it's gone, flag as a regression — don't fix the manifest without sign-off, it changes what's exposed inside a running container |

---

## Output

Produce a unified summary covering every audit that ran (mark any skipped
part and why):

```
KB Audit:           N files reviewed, X actions taken (Y updates, Z deletes, W creates)
Memory Audit:       [skipped — no auto-memory dir found] OR N entries reviewed, X stale, Y conflicts resolved, Z gaps filled
Skill Health:       N skills checked, X low-quality, Y unused, Z unregistered-in-index
KB Reachability:    N agents/skills checked, X missing KB instruction, Y missed-skill patterns found, Z KB entries repositioned
```

Use `mcp__aw-gateway__aw_presentation__create_presentation` to render a
visual report when running interactively, if that MCP tool is available.

### Mandatory: send the summary to Telegram

Producing the report is not the same as delivering it — when this runs
unattended (a scheduled task, no human watching), the summary above is
worthless unless it actually reaches Frederico. Nothing else in this skill
notifies anyone. Always run this as the last step, even on a clean run
with zero actions taken:

```bash
curl -s -m 15 -X POST http://172.18.0.1:10014/api/telegram/report \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json, sys
print(json.dumps({"title": sys.argv[1], "text": sys.argv[2]}))
' "kb-curator — $(date +%Y-%m-%d)" "$YOUR_SUMMARY_TEXT")"
```

`172.18.0.1:10014` is the same Agents Platform address used by
`aw-app-tasks`'s own `agents_platform_base` config (the bridge gateway —
see the aw-system-analyst skill for the same pattern). If the curl fails,
say so explicitly in your final output rather than silently retrying —
a failed notification is itself worth knowing about.

All proposed destructive actions (delete/merge) should go through whatever
approval channel this workspace uses before executing — don't assume a
specific mechanism exists; ask if none is obvious.

---

## MCP tools used

| Tool | Purpose |
|---|---|
| `mcp__aw-gateway__aw_knowledge_base__search_knowledge_base` | Verify a topic exists before flagging as gap |
| `mcp__aw-gateway__aw_knowledge_base__update_knowledge_base` | Create or update a KB doc |
| `mcp__aw-gateway__aw_knowledge_base__delete_knowledge_base` | Remove stale/orphaned KB doc |
| `aw__agents_platform_runners__list_retro_scores` | Fetch retro scores for all agents (if installed) |
| `aw__agents_platform_runners__list_agents` | Full agent list for cross-check (if installed) |
| `aw__agents_platform_runners__list_target_runs` | Check run recency for an agent's target (if installed) |
| `aw__agents_platform_runners__peek_run_output` | Inspect run output for missed-skill patterns (if installed) |
| `mcp__aw-gateway__aw_presentation__create_presentation` | Visual audit report (if installed) |

`search_skills` / `load_skill` are listed in this app's own `contributes.mcp`
but are currently non-functional — see 4.4.

---

## Knowledge Base path conventions

| Path pattern | Use for |
|---|---|
| `memory/<topic>.md` | Agent lessons, bug fixes, architecture discoveries |
| `docs/<area>/<topic>.md` | Reference documentation |
| `skills/<skill-name>.md` | Skill documentation mirrors (distinct from the live `/opt/aw-workspace/skills/<id>/SKILL.md` — this is a KB *copy* for search, not the source of truth) |

These are conventions enforced by usage, not by the app — `update_knowledge_base`
accepts any relative path. Keep to them so search stays predictable.
