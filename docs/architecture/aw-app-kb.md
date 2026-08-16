---
repo: architecture
path: docs/architecture/aw-app-kb.md
source: generated
edited: false
checksum: sha256:c4605857a20b2366ad5bf4b9bfed1f9a33f822a7db15e5556ded426b4e34f3a2
---
# Knowledge Base

- **repo**: aw-app-kb
- **layer**: app-container
- **technologies**: react, docker
- **health** (derived): planned

Semantic search over project docs and skills, backed by Postgres/pgvector. Ported from the agentic-workspace monolith's Knowledge Base — file browser/editor, semantic + text search, code-map build jobs, and an MCP surface (search/update/delete_knowledge_base, search/load_skill) that aw-mcp-gateway picks up automatically.

## Connections
- `stdio-mcp` → **mcp-gateway** — MCP surface aggregated by the gateway

## MCP tools
- `delete_knowledge_base`
- `load_skill`
- `search_knowledge_base`
- `search_skills`
- `update_knowledge_base`

## Requirements
_none documented_
