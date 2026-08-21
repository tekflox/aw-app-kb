---
repo: architecture
path: docs/architecture/aw-app-kb.md
source: generated
edited: false
checksum: sha256:25a8761bfafcb1b5e31b1b2792a29a20fad8bb8a07303276b090d9c193ad6063
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
### Pasta desligada é opt-OUT, e o workspace segue sendo a fonte do que existe
- Given o usuário desliga uma pasta mapeada da indexação e mais tarde mapeia uma pasta nova no workspace
- When as settings resolvem quais pastas indexar a partir da lista de exceções, não de uma lista de inclusão (repos/aw-app-kb/kb_app/settings.py::get_settings:29, consumo em repos/aw-app-kb/kb_app/kb_ops.py::_map_all:1289 na linha 1318)
- Then a pasta nova entra indexada por padrão e só as desligadas explicitamente ficam de fora — com lista de inclusão toda pasta mapeada depois é ignorada em silêncio, e a KB responde busca sem ela sem nada indicar que existe conteúdo que ela nunca viu
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-kb/tests/test_routes.py` (passing)

### --map-all é sincronização: saída de pasta desmapeada é podada
- Given uma pasta foi desmapeada ou renomeada (o que é um desmapear mais um mapear) e sua árvore gerada continua sob mapped_folders/
- When o map-all seguinte compara o que existe em disco com o conjunto esperado (repos/aw-app-kb/kb_app/kb_ops.py::_prune_unmapped_output:1367)
- Then só as árvores sob mapped_folders/ que sobraram são removidas, e nada que o usuário escreveu à mão em outro lugar da KB é tocado — sem a poda a KB segue listando e respondendo buscas sobre pastas que não existem mais, como aw-docs, agentic-workspace e evidencia depois do rename de 13/08, e a resposta parece atual porque nada marca o documento como órfão
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-kb/tests/test_routes.py` (passing)

### Caminho de host do workspace é traduzido para onde ele está montado aqui
- Given alguém digita /opt/aw-workspace/repos/&lt;algo&gt;, que é o caminho que o workspace exibe em todo lugar mas não resolve dentro deste container
- When o alvo é resolvido antes de ser procurado (repos/aw-app-kb/kb_app/kb_ops.py::_translate_host_path:992, chamado por ::_resolve_map_target:1026)
- Then o caminho vira o mount equivalente e a operação segue, enquanto pasta mapeada continua endereçada pelo NOME que o workspace registrou e nunca por caminho de host — sem a tradução o pedido morre como "Repo not found" acompanhado de uma lista de nomes que não explica por que o caminho certo falhou
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-kb/tests/test_kb_ops.py` (passing)

### Toda operação de arquivo da KB é confinada ao KB_DIR
- Given uma requisição de ler, salvar ou apagar traz um path com ../ ou um caminho absoluto
- When o path é resolvido com realpath e comparado ao KB_DIR real antes de qualquer I/O (repos/aw-app-kb/kb_app/routes.py::KBRoutes._safe_path:133, usado em ::save_file:172, ::read_file:166 e ::delete_file:199)
- Then a resolução devolve None e a rota recusa — a app roda com o filesystem do container montado, então uma escrita que escape do KB_DIR alcança config e dados de outras apps por uma rota cuja função declarada é editar documentação
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-kb/tests/test_routes.py` (passing)
