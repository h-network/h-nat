# h-orchestrator

`h-orchestrator` exposes stateless command-line agents as NeMo Agent Toolkit
(NAT) functions. The first implementation slice provides:

| NAT `_type` | Behavior |
| --- | --- |
| `h_agent_invoke` | Execute a configured command in an OpenShell sandbox and return parsed output. |
| `h_agent_stream` | Execute a configured command and yield stdout chunks. |
| `claude_invoke` | Invoke the Claude CLI through the unary core with stateless JSON-mode defaults. |

Conversation memory is not implicit. Compose these functions with `h-memory`
or another state provider at the workflow/application layer.

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

See [HLD.md](HLD.md) for the module boundary and [LLD.md](LLD.md) for the
current implementation details and remaining baseline work.

Report orchestration, parsing, or wrapper defects to h-network. Report stable
plugin API defects to NVIDIA NeMo Agent Toolkit and gateway transport defects
to `h-openshell`.
