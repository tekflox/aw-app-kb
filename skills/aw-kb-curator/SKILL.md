---
name: aw-kb-curator
description: Curate the AW knowledge base, agent memory, and skill health — review docs/notes for staleness/duplicates/gaps, audit memory entries for conflicts and drift, and flag skills with low retro scores or no recent use. Use when the user says "revisa o KB", "curate knowledge base", "kb-curator", "revisa a memória" or when running the weekly knowledge audit.
---

# aw-kb-curator — Knowledge Base, Memory & Skill Health Curator

Three audits in one run:

1. **KB Audit** — curateable docs/notes for staleness, duplicates, gaps
2. **Memory Audit** — auto-memory entries for drift, conflicts, gaps
3. **Skill Health** — agents-platform retro scores + unused/orphaned skills

Run all three every time. Produce a unified report at the end.

---

## Part 1 — KB Audit

### Scope

| Dir | Contents |
|---|---|
| `docs/knowledge_base/skills/` | Skill SKILL.md docs mirrored from `skills/` |
| `docs/knowledge_base/docs/` | Standalone architecture/platform docs |

**Excluded directories:**
- `docs/knowledge_base/notes/` — Notion-synced, read-only from this agent's perspective (edits are overwritten in 30 min; deletes archive Notion pages). Do not touch.
- `docs/knowledge_base/mapped_folders/` — auto-generated.

### Listing files

```bash
./skills/aw-kb-curator/list-kb-files.sh
```

One path per line. Each file has YAML frontmatter: `source`, `last_edited`, `path`, `checksum`.

### Curation workflow

**Step 1 — Inventory pass (metadata only)**

Read frontmatter of each file. Flag:

| Flag | Criterion |
|---|---|
| `stale` | `last_edited` older than 60 days AND source is `notion` |
| `orphan-skill` | In `skills/` subfolder but `skills/<name>/` no longer exists |
| `duplicate` | Two files with very similar titles or paths |
| `gap` | Topic referenced in other docs but no dedicated doc exists |

**Step 2 — Content read (flagged files only)**

For each flagged file decide:
- **Update** — outdated content → `update_knowledge_base`
- **Delete** — stale/orphaned/superseded → `delete_knowledge_base`
- **Merge** — near-duplicate → keep better one, delete other
- **Create** — gap → draft new doc via `update_knowledge_base`

**Step 3 — Skill docs cross-check**

Compare `docs/knowledge_base/skills/` against live `skills/` directory:

```bash
ls /opt/agentic-workspace/skills/
```

KB skill doc with no matching live `skills/<name>/` → mark as orphan to delete.

---

## Part 2 — Memory Audit

Memory files live at:

```
/home/ubuntu/.claude/projects/-opt-agentic-workspace/memory/
```

### Workflow

**Step 1 — Read index**

```bash
cat /home/ubuntu/.claude/projects/-opt-agentic-workspace/memory/MEMORY.md
```

This is the index. Each line points to a memory file.

**Step 2 — Read all memory files**

Read every `.md` file listed in MEMORY.md. Note: memory files have frontmatter with `type` (`user`, `feedback`, `project`, `reference`) and `description`.

**Step 3 — Flag issues**

| Flag | Criterion |
|---|---|
| `stale` | `project` type memory referencing a date/event that has clearly passed, OR referencing code constructs that may have changed (verify against current codebase) |
| `conflict` | Two memories that give contradictory guidance on the same topic |
| `outdated-fact` | Memory claims X exists/works a certain way — verify by reading the current code/file |
| `gap` | Important behavioral pattern or preference observed in recent sessions but not captured |

**Step 4 — Act**

- **Update stale memory**: edit the file with corrected content using the Write tool.
- **Delete obsolete memory**: remove the file AND remove its line from MEMORY.md.
- **Resolve conflict**: merge into one authoritative entry, delete the other.
- **Create new memory**: write a new `.md` file and add it to MEMORY.md.

Always keep MEMORY.md in sync — every add/delete must update the index.

Memory types and format: see frontmatter convention in existing files (`type`, `name`, `description` + body with **Why:** and **How to apply:** for feedback/project types).

---

## Part 3 — Skill Health Review

### Fetch retro scores

Use the agents-platform MCP tool to get retro scores for all agents:

```
mcp__aw-gateway__agents_platform__list_retro_scores
```

### Cross-check live skills

```bash
ls /opt/agentic-workspace/skills/
```

For each entry in `skills/`, check:
1. Is there a corresponding agent in agents-platform? If not, the skill is **unregistered**.
2. Does the agent have recent runs? If no runs in the last 30 days, it's **unused**.
3. Is the retro score below 0.5? Flag as **low-quality**.

### Skill doc freshness

For each `skills/<name>/SKILL.md`, check `last_modified` vs the agents-platform `updated_at` for the matching agent. If they diverge significantly, the skill doc may be **out of sync** with the agent prompt.

### Actions

| Flag | Action |
|---|---|
| `low-quality` | Read the SKILL.md + recent run lessons → propose specific improvements |
| `unused` | Determine if the skill is obsolete or just not triggered — propose deprecation or trigger condition update |
| `unregistered` | Propose creating the matching agent in agents-platform |
| `out-of-sync` | Read both SKILL.md and agent system_prompt → propose which is the source of truth and update the other |

---

## Part 4 — KB Reachability Audit

> **Goal**: confirm that KBs and skills being created are actually reachable by agents — i.e. the agent will find them at runtime, not just that the doc exists.

### 4.1 — Check which agents carry the KB search instruction

Agents can only use the KB if their prompt tells them to. Inspect the three surfaces:

**a) AGENTS.md (global CLIs — Claude Code, Cursor, Codex, Gemini):**

```bash
grep -n "search_knowledge_base\|KB" /opt/agentic-workspace/AGENTS.md | head -20
```

**b) agents-platform agents (via API):**

```bash
curl -s http://127.0.0.1:10005/api/agents | python3 -c "
import json,sys
agents = json.load(sys.stdin)
for a in agents:
    has_kb = 'search_knowledge_base' in (a.get('system_prompt') or '')
    print(f\"{'✓' if has_kb else '✗'} {a['slug']}: {a['name'][:50]}\")
"
```

**c) Bot/CLI skills — check Telegram and other bots:**

```bash
grep -rn "search_knowledge_base\|aw-knowledge-base" /opt/agentic-workspace/skills/ | grep -v ".pyc"
```

Flag any agent/bot whose system_prompt **does not** contain `search_knowledge_base` or a reference to the KB.

### 4.2 — Analyze agent run histories for missed skills

Look for sessions where a skill-relevant query was made but the skill was never loaded. Signals:

- Agent improvised instructions instead of following a known SKILL.md
- User had to re-explain something already documented in a skill
- A skill's trigger keywords appeared in the conversation but `load_skill` / `cat SKILL.md` was never called

Check recent Telegram messages via the MCP:

```
mcp__aw-gateway__aw_telegram__list_messages(limit=50)
```

And recent agents-platform run outputs (pick the last 5-10 runs per active agent):

```
mcp__aw-gateway__agents_platform__list_target_runs(target_slug=<slug>, limit=10)
mcp__aw-gateway__agents_platform__peek_run_output(run_id=<id>)
```

Look for patterns like:
- Skill keywords in user input but no `cat skills/*/SKILL.md` in the run output
- KB search not called at session start
- Agent asked the user for info that already exists in a KB doc

### 4.3 — Verify KB entries are positioned for discoverability

For each new KB entry created in this session or recently (`last_edited` < 7 days):

1. Search for it: `search_knowledge_base(query=<topic>)` — does it appear in top-3?
2. If not, check if the path/title is too generic or the content lacks keywords
3. Rewrite the KB entry with better trigger vocabulary: include the exact phrases agents would search for

### 4.4 — Actions

| Finding | Action |
|---|---|
| Agent missing KB instruction | Propose adding `search_knowledge_base` call to its system_prompt (via `update_agent`) — flag for Frederico approval |
| Missed skill in run history | Add a KB entry under `memory/<skill-name>-trigger-patterns.md` mapping the user's natural-language phrasing to the skill |
| KB entry not discoverable | Rewrite via `update_knowledge_base` with richer keyword coverage |
| Bot (e.g. Telegram) not reading KB | Check `skills/aw-agent-telegram/SKILL.md` — if no KB section exists, propose adding one |

---

## Output

Produce a unified summary covering all four audits:

```
KB Audit:           N files reviewed, X actions taken (Y updates, Z deletes, W creates)
Memory Audit:       N entries reviewed, X stale, Y conflicts resolved, Z gaps filled
Skill Health:       N skills checked, X low-quality, Y unused, Z unregistered
KB Reachability:    N agents checked, X missing KB instruction, Y missed-skill patterns found, Z KB entries repositioned
```

Use `mcp__aw-gateway__aw_presentation__create_presentation` to render a visual report when running interactively.

All proposed destructive actions (delete/merge) go into TargetLessons with category `opportunity` pending Frederico's approval via Telegram `[[OPTIONS]]` before execution.

---

## MCP tools used

| Tool | Purpose |
|---|---|
| `mcp__aw-gateway__aw_knowledge_base__search_knowledge_base` | Verify a topic exists before flagging as gap |
| `mcp__aw-gateway__aw_knowledge_base__update_knowledge_base` | Create or update a KB doc |
| `mcp__aw-gateway__aw_knowledge_base__delete_knowledge_base` | Remove stale/orphaned KB doc |
| `mcp__aw-gateway__aw_knowledge_base__search_skills` | Semantic search across skills by task description |
| `mcp__aw-gateway__aw_knowledge_base__load_skill` | Load full SKILL.md content by skill name |
| `mcp__aw-gateway__agents_platform__list_retro_scores` | Fetch retro scores for all agents |
| `mcp__aw-gateway__agents_platform__list_agents` | Full agent list for cross-check |
| `mcp__aw-gateway__agents_platform__list_target_runs` | Check run recency for an agent's target |
| `mcp__aw-gateway__agents_platform__peek_run_output` | Inspect run output for missed-skill patterns |
| `mcp__aw-gateway__agents_platform__update_agent` | Patch agent system_prompt to add KB instruction |
| `mcp__aw-gateway__aw_telegram__list_messages` | Analyze Telegram history for missed skill triggers |
| `mcp__aw-gateway__aw_presentation__create_presentation` | Visual audit report |

---

## Knowledge Base path conventions

| Path pattern | Use for |
|---|---|
| `memory/<topic>.md` | Agent lessons, bug fixes, architecture discoveries |
| `docs/<area>/<topic>.md` | Reference documentation |
| `skills/<skill-name>/SKILL.md` | Skill documentation |

**Never write to `docs/knowledge_base/<topic>.md` directly** (root level).

If you find a KB entry at the root level, move it via `update_knowledge_base` (new path) + `delete_knowledge_base` (old path).

---

## Running as a scheduled agent

- **Agent slug**: `kb-curator`
- **Trigger**: weekly aw-task (`weekly-kb-audit`)
- **Output**: TargetLessons with category `opportunity` for each proposed change
- **Approval**: Frederico reviews via Telegram `[[OPTIONS]]` before any destructive action
