# h-nat

NeMo Agent Toolkit plugins for agent orchestration, memory, and Asimov-gated safety — with optional NVIDIA OpenShell sandboxing.

Five composable plugins, install what you need:

- **h-openshell** — gRPC client for NVIDIA OpenShell sandboxes.
- **h-orchestrator** — invoke/stream a coding-agent CLI as a NAT function.
- **h-memory** — bounded per-chat conversation memory in Redis.
- **h-recall** — long-term semantic memory, hybrid search over vectorized history.
- **h-asimov** — pre-flight safety gate: denylist + LLM judge, before anything executes.

## Agents

### Reference specialists (knowledge only, no code)

| Lane                          | Agent           | CLI    | Profile  |
|-------------------------------|-----------------|--------|----------|
| h-network-nemo-agent-toolkit  | claude-nat      | claude | bussines |
| h-nemo-stack                  | codex-nemostack | codex  | default  |
| NVIDIA NeMo-Agent-Toolkit     | sme-nat         | claude | bussines |
| NVIDIA OpenShell              | sme-openshell   | codex  | default  |

### Module builders

| Module         | Agent               | CLI    | Profile   |
|----------------|---------------------|--------|-----------|
| h-openshell    | mod-h-openshell     | codex  | default   |
| h-orchestrator | mod-h-orchestrator  | codex  | default   |
| h-memory       | mod-h-memory        | agy    | default   |
| h-recall       | mod-h-recall        | agy    | default   |
| h-asimov       | mod-h-asimov        | claude | bussines  |

### Testing

| Lane       | Agent      | CLI | Profile  |
|------------|------------|-----|----------|
| Acceptance | acceptance | agy | bussines |
