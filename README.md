# h-nat

NeMo Agent Toolkit plugins for agent orchestration, memory, and Asimov-gated safety — with optional NVIDIA OpenShell sandboxing.

Five composable plugins, install what you need:

- **h-openshell** — gRPC client for NVIDIA OpenShell sandboxes.
- **h-orchestrator** — invoke/stream a coding-agent CLI as a NAT function.
- **h-memory** — bounded per-chat conversation memory in Redis.
- **h-recall** — long-term semantic memory, hybrid search over vectorized history.
- **h-asimov** — pre-flight safety gate: denylist + LLM judge, before anything executes.
