# h-orchestrator high-level design

## Status

This document describes the intended target design for `h-orchestrator` in
`h-nat`. The implementation has not landed in this repository yet. The
starting specification is the existing
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

The predecessor provides the following NAT function types. These are the
candidate target surface for the port, subject to review while the public API
is implemented.

| Function type | Contract | Responsibility |
| --- | --- | --- |
| `h_agent_invoke` | `str -> str` | Run a prompt through a configured command and parse its final output. |
| `h_agent_stream` | `str -> AsyncGenerator[str, None]` | Run a configured command and yield stdout chunks. |
| `claude_invoke` | `str -> str` | Apply Claude CLI defaults to the generic invocation path. |
| `claude_stream` | `str -> str` | Consume Claude stream-json events and return the final assistant text. |
| `claude_via_hramp` | `str -> str` | Dispatch a Claude CLI command through h-ramp without memory composition. |
| `h_chat_cycle` | typed chat input -> typed chat output | Read bounded history, call a configured NAT dispatcher, and persist the new turn. |
| `h_claude_cycle` | typed chat input -> typed chat output | Read bounded history, dispatch Claude through h-ramp, and persist the new turn. |

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
                                             or -> h-ramp -> agent peer
```

`h-orchestrator` fits between NAT workflows and an execution transport:

- `h-openshell` provides the async gRPC/mTLS sandbox execution client used by
  the generic invoke and stream paths.
- h-ramp is an optional dispatch path for agent peers. It is used by the
  predecessor's Claude dispatcher and Claude-specific cycle.
- `h-memory` owns bounded conversation storage. Stateless invocation never
  imports memory concerns; the explicit cycle layer composes with it.
- `h-recall` may supply long-term context at the caller/workflow layer. It is
  not an implicit dependency of an invocation.
- NAT supplies plugin discovery, configuration models, function registration,
  type conversion, and tracing integration.

## Design boundaries

- Agent processes are stateless by default; conversational continuity is
  supplied by explicit composition.
- The generic core is command- and agent-agnostic. Claude behavior belongs in
  wrappers or dispatchers.
- Infrastructure clients are created lazily, so loading a workflow does not
  require a live gateway, Redis server, or h-ramp endpoint.
- Configuration rejects unknown fields to expose stale workflow configuration
  instead of silently ignoring it.
- Prompts and command arguments are shell-quoted before execution. Transport
  security and sandbox policy remain the responsibility of the transport
  module and deployment.
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
5. Transport-specific dispatchers such as h-ramp.
6. Explicit memory-aware composites and their wire-shape converters.

This separation lets another CLI, parser, or dispatcher be added without
changing the generic execution path or coupling one-shot invocation to memory.

