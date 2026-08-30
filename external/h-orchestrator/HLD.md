# h-orchestrator high-level design

## Status

This document describes the target design for `h-orchestrator` in `h-nat`.
Stateless invocation, Claude streaming, and the generic memory-aware composite
are implemented. The starting specification is the existing
`h-network-nemo-agent-toolkit/external/h-network-orchestrator` plugin; the port
may simplify or refactor that design before it becomes the public
`h-orchestrator` interface.

## Purpose

`h-orchestrator` makes command-line agents available as composable NeMo Agent
Toolkit (NAT) functions. It owns prompt delivery, remote execution lifecycle,
streaming, and output interpretation. It also provides agent-specific wrappers
and optional higher-level chat-cycle functions.

The core invocation path is deliberately stateless. It does not retrieve or
write conversation memory. A caller can use it for a one-shot command or
compose it with `h-memory` and `h-recall`. Higher-level cycle functions may
offer that composition explicitly, so the stateful and stateless contracts are
not confused.

## Public interface

Six NAT function types are implemented. h-ramp-dependent predecessor
functions are deferred because h-ramp has no public package contract in the
five-module `h-nat` plan.

| Function type | Status | Contract | Responsibility |
| --- | --- | --- | --- |
| `h_agent_invoke` | Implemented | `str -> str` | Run a prompt through a configured command and parse its final output. |
| `h_agent_stream` | Implemented | `str -> AsyncGenerator[str, None]` | Run a configured command and yield stdout chunks. |
| `claude_invoke` | Implemented | `str -> str` | Apply Claude CLI defaults to the generic invocation path. |
| `claude_stream` | Implemented | `str -> str` | Consume Claude stream-json events and return the final assistant text. |
| `h_chat_cycle` | Implemented | typed chat input -> typed chat output | Read bounded history, call a configured NAT dispatcher, and persist the new turn. |
| `h_gated_mcp_tool` | Implemented | live MCP input schema -> typed gated result | Wrap one non-public MCP group member and enforce `h_asimov_gate` before nested invocation. |
| `claude_via_hramp` | Deferred | `str -> str` | Would dispatch a Claude CLI command through h-ramp without memory composition. |
| `h_claude_cycle` | Deferred | typed chat input -> typed chat output | Would read bounded history, dispatch Claude through h-ramp, and persist the new turn. |

The generic invocation configuration selects an execution target, command,
arguments, prompt-delivery method (`arg`, `stdin`, or `env:VARNAME`), optional
static context, timeout, and output parser. Agent wrappers pre-fill these
fields without changing the underlying lifecycle.

Unary output parsing is extensible. The predecessor includes `raw` and
`claude_json` parsers and discovers external parsers through the
`nat.orchestrator.output_parsers` Python entry-point group. The public port
should retain an explicit, small parser contract rather than embedding
agent-specific parsing in the generic core.

## System context

```text
NAT workflow / API caller
          |
          +-- stateless invoke/stream --------------------------+
          |                                                     |
          |                                      h-openshell -> sandbox CLI
          |                                                     |
          +-- explicit chat cycle -> h-memory / Redis           |
                                  -> dispatcher ----------------+
          |
          +-- gated MCP member -> h-asimov -> hidden mcp_client member
```

`h-orchestrator` fits between NAT workflows and an execution transport:

- `h-openshell` provides the async gRPC/mTLS sandbox execution client used by
  the generic invoke and stream paths.
- h-ramp is not currently a dependency. Support is deferred pending a decision
  on whether it becomes a sixth public `h-nat` module.
- `h-memory` owns bounded conversation storage. Stateless invocation never
  imports memory concerns; the explicit cycle layer composes with it.
- `h-recall` may supply long-term context at the caller/workflow layer. It is
  not an implicit dependency of an invocation.
- `h-asimov` supplies the pure ALLOW/DENY judge required by gated MCP wrappers.
- NAT supplies plugin discovery, configuration models, function registration,
  type conversion, and tracing integration.
- `nvidia-nat-mcp` supplies MCP discovery, transport, and live member schemas.
  An explicit group allowlist separates public inspection members from hidden
  execution-capable members used only by gated wrappers.

Direct in-process SSH is intentionally outside this module. The former
`h_ssh_exec` function was removed after external tool access for OpenAI-style
agents standardized on MCP; `examples/gated-junos-mcp` is the supported
composition.

## Design boundaries

- Agent processes are stateless by default; conversational continuity is
  supplied by explicit composition.
- The generic core is command- and agent-agnostic. Claude behavior belongs in
  wrappers or dispatchers.
- Infrastructure clients are created lazily, so loading a workflow does not
  require a live gateway or Redis server.
- Configuration rejects unknown fields to expose stale workflow configuration
  instead of silently ignoring it.
- Prompts and command arguments are shell-quoted before execution. Transport
  security and sandbox policy remain the responsibility of the transport
  module and deployment.
- Direct SSH is a separate typed function, not a mode of the sandbox execution
  core. Its agent input excludes credentials, and its deployment defaults to
  SSH host-key verification and fail-closed authorization.
- Failures need one documented contract per public function. The predecessor
  sometimes returns errors as strings; the port should preserve that only
  where compatibility requires it and otherwise prefer typed, observable
  failures.

## Target component boundaries

The implementation should keep these concerns separate:

1. NAT registration and strict configuration models.
2. Generic script construction and invocation lifecycle.
3. Unary output-parser protocol and parser discovery.
4. Thin agent-specific wrappers.
5. Transport-specific dispatchers, if their transport has a public contract.
6. Explicit memory-aware composites and their wire-shape converters.

This separation lets another CLI, parser, or dispatcher be added without
changing the generic execution path or coupling one-shot invocation to memory.

## Production composition example

`examples/hot-memory-recall-tool` demonstrates the intended two-tier memory
composition. `h_chat_cycle` always supplies recent `h-memory` turns to a
configured NAT `tool_calling_agent`. The agent uses an OpenAI-compatible LLM
and receives `h_semantic_search` as an optional tool, so the model—not the
outer cycle—decides when long-term recall is necessary. h-recall migration and
vectorization remain explicit operator-scheduled maintenance operations.

The example fixes one chat/tenant identity across the hot store, audit store,
and tool instructions. Its verifier seeds a random fact, migrates it out of hot
memory, asserts that a self-contained question does not call recall, then
asserts that an older-fact question calls recall and returns the random fact.

## Plain-chat memory example

`examples/plain-chat-memory` demonstrates the minimal stateful composition
independently of tools and network operations. `h_chat_cycle` reads and writes
bounded Redis history around NAT's standard `chat_completion` dispatcher. A
ten-process driver plants four personal facts, checks an unrelated arithmetic
question, recalls each fact, requests a summary, and verifies the hot-index
record count and persisted roles directly in Redis.
