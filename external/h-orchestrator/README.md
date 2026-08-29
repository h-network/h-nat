# h-orchestrator

`h-orchestrator` exposes command-line agents and explicit conversation cycles
as NeMo Agent Toolkit (NAT) functions:

| NAT `_type` | Behavior |
| --- | --- |
| `h_agent_invoke` | Execute a configured command in an OpenShell sandbox and return parsed output. |
| `h_agent_stream` | Execute a configured command and yield stdout chunks. |
| `claude_invoke` | Invoke the Claude CLI through the unary core with stateless JSON-mode defaults. |
| `claude_stream` | Consume Claude stream-json incrementally and return its final result. |
| `h_chat_cycle` | Read bounded Redis history, call a configured dispatcher, and persist the successful turn. |

Conversation memory is not implicit in invoke/stream functions. Use
`h_chat_cycle` when the workflow should explicitly compose `h-memory` around a
dispatcher.

## Install and test

The package supports NVIDIA NeMo Agent Toolkit 1.8 through the current 1.x
series and Python 3.11-3.13.

```bash
pip install .
pip install -e ".[test]"
pytest
```

`h-openshell` supplies gateway discovery and mTLS credentials. Configure its
OpenShell home or the optional `gateway_home`, `endpoint`, and
`target_override` workflow fields; credentials are not invocation inputs.

## Generic invocation

```yaml
workflow:
  _type: h_agent_invoke
  sandbox: my-sandbox
  command: bash
  args: ["-c"]
  prompt_via: arg
  output_parser: raw
```

The prompt can be delivered as a final command argument, stdin heredoc, or a
validated environment variable. Optional `context` is a static prefix and is
separated from the prompt by a blank line.

Built-in unary parsers are `raw` and `claude_json`. Third-party packages can
publish parsers through the `nat.orchestrator.output_parsers` entry-point
group. Streaming currently yields raw stdout and does not use this parser
registry.

## Claude wrapper

```yaml
workflow:
  _type: claude_invoke
  sandbox: my-sandbox
```

The wrapper supplies `claude -p --no-session-persistence` and JSON output
defaults. Operators may override the inherited command arguments or add a
Claude settings file with `hook_settings_path`.

Use `_type: claude_stream` with the same sandbox fields to consume Claude
stream-json. It handles JSON and UTF-8 sequences split across transport chunks
and returns the final successful result.

## Memory-aware chat cycle

```yaml
functions:
  agent:
    _type: claude_invoke
    sandbox: my-sandbox

workflow:
  _type: h_chat_cycle
  dispatcher: agent
  chat_id: example-chat
  pod: example
  agent: assistant
  redis_url: redis://localhost:6379
```

`chat_id`, `pod`, and `agent` can instead be supplied per request and override
configuration defaults. The composite reads prior live turns oldest-first,
builds a history prompt, calls the configured NAT function using its
`str -> str` contract, and writes user and assistant turns only after a
successful dispatch.

h-ramp-dependent functions are not included. h-ramp has no public contract in
the five-module `h-nat` plan; support is deferred pending a module decision.

See [HLD.md](HLD.md) for the module boundary and [LLD.md](LLD.md) for the
current implementation details and remaining baseline work.

Report orchestration, parsing, or wrapper defects to h-network. Report stable
plugin API defects to NVIDIA NeMo Agent Toolkit and gateway transport defects
to `h-openshell`.
