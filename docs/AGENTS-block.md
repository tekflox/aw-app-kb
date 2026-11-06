**Always call `search_knowledge_base` (via the workspace MCP gateway) at the
start of every non-trivial task and before invoking any tool that touches the
codebase, infrastructure, or project decisions.** This surfaces relevant docs,
lessons learned, architecture notes, and prior decisions that would otherwise
be invisible.

When to search (mandatory):
- Any new user request or task — use the user's message as the query
- Before reading/editing a file you haven't seen this session — query the file
  path or topic first
- Before proposing an architecture or design — query the subject area
- When unsure how something works in this project — query it before guessing

How to search well:
- Prefer short, focused queries (3–8 words) over long ones
- Run 2–3 searches with different angles if the first yields thin results
- If results contain a relevant doc, read it before proceeding

This is not optional. Skipping the KB search is the single biggest cause of
repeated mistakes and re-doing work that was already documented.

The tool name you see depends on the gateway: `search_knowledge_base`
directly, or `aw__kb__search_knowledge_base` when routed through
`aw-mcp-gateway`. A flaky or reconnecting MCP server is not the same as an
absent one; retry once before giving up, and say so in your answer if you
proceeded without the KB.
