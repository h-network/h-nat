# h-orchestrator low-level design

## Document contract

This is the canonical low-level description of the code currently in
`external/h-orchestrator`. It is not an aspirational design. Changes to package
layout, public Python helpers, NAT registrations, configuration, parser
behavior, execution lifecycle, or tests must update this document in the same
branch.

`HLD.md` owns the durable module boundary and target public intent. This file
owns the mechanisms that are implemented now.

## Implementation status

The first stateless execution slice is implemented. It contains an installable
Python package, generic unary and streaming NAT functions, a Claude unary
wrapper, two unary parsers, and transport-neutral unit tests.

The predecessor's `claude_stream`, `claude_via_hramp`, `h_chat_cycle`, and
`h_claude_cycle` functions have not been ported. There is no Redis, `h-memory`,
or h-ramp dependency in the implemented runtime path.

## Package layout

```text
external/h-orchestrator/
├── HLD.md
├── LLD.md
├── README.md
├── pyproject.toml
├── src/nat/plugins/h_orchestrator/
│   ├── __init__.py
│   ├── core.py
│   ├── register.py
│   └── parsers/
│       ├── __init__.py
│       ├── raw.py
│       └── claude_json.py
└── tests/
    ├── conftest.py
    ├── test_core.py
    ├── test_parsers.py
    └── test_register.py
```

The distribution is `h-orchestrator`, version `0.1.0`, supporting Python
3.11-3.13. Runtime dependencies are `h-openshell`,
`nvidia-nat-core>=1.8,<2`, and Pydantic 2. The namespace package is
`nat.plugins.h_orchestrator`. Plugin-authoring symbols are imported from the
stable `nat.plugin_api` facade.

NAT loads `nat.plugins.h_orchestrator.register` through the `h_orchestrator`
entry point in the `nat.plugins` group. The distribution also publishes its
built-in parsers in `nat.orchestrator.output_parsers`.

## Public Python surface

`nat.plugins.h_orchestrator` exports:

- `ParseResult`, the immutable normalized unary result;
- `OutputParser`, the runtime-checkable unary parser protocol;
- `build_script`, the quoted bash-script builder; and
- `with_context`, the static-context prompt composer.

NAT configuration and registration symbols remain available from
`nat.plugins.h_orchestrator.register` but are not re-exported as the supported
library surface.

## NAT registrations

### `h_agent_invoke`

`AgentInvokeConfig` rejects unknown fields and contains:

- `gateway_home: str | None = None`;
- `endpoint: str | None = None`;
- `target_override: str = "localhost"`;
- required non-empty `sandbox` and `command`;
- `rpc_timeout_seconds: float = 600.0`, constrained above zero;
- `args: list[str] = []`;
- `prompt_via: str = "arg"`, restricted to `arg`, `stdin`, or a valid
  `env:VARNAME` shape;
- `context: str | None = None`; and
- `output_parser: str = "raw"`.

The builder resolves the parser during workflow construction but does not read
OpenShell configuration or open a client. The first invocation constructs
`OpenShellClient.from_default_home`, optionally prepends static context, builds
the script, and calls the parser. The reserved parser `step_manager` argument
is currently `None`; intermediate-step internals are not part of the stable
external plugin API.
Successful parse text is returned unchanged. Parser failure currently returns
`error_message` as data and emits a warning.

The client is closed in the builder's `finally` block if it was constructed.

### `h_agent_stream`

`AgentStreamConfig` inherits the complete strict unary configuration. The
builder constructs its client lazily, calls `exec_stream`, and:

- yields UTF-8 replacement-decoded stdout payloads;
- logs up to 300 characters of each decoded stderr payload;
- records the exit payload; and
- yields `[exit_code=N]` for a non-zero exit or
  `[exit_code=missing]` if no terminal event arrives.

The accepted `output_parser` field is not consulted on this path. Streaming is
raw stdout only. The concrete nested return annotation is
`AsyncGenerator[str, None]`, so `register.py` intentionally does not enable
postponed evaluation of annotations.

### `claude_invoke`

`ClaudeInvokeConfig` inherits the unary config and supplies:

- `command="claude"`;
- positional prompt delivery;
- `output_parser="claude_json"`; and
- flags for print mode, disabled session persistence and slash commands, Bash
  tools, bypass-permissions mode, and JSON output.

When `hook_settings_path` is present, a Pydantic post-validator appends
`--settings <path>` to the resolved argument list. The registration delegates
to the same unary builder as `h_agent_invoke`.

## Script construction

`build_script` returns UTF-8 bytes beginning with `set -e`. The command and
every argument are individually shell-quoted with `shlex.quote`.

- `arg` appends the quoted prompt as the final positional argument.
- `stdin` sends the prompt through a single-quoted heredoc. The delimiter
  starts as `__H_AGENT_PROMPT__` and gains a numeric suffix until it does not
  equal any complete prompt line, preventing prompt-controlled early closure.
- `env:VARNAME` exports the quoted prompt, validates the variable name again
  defensively, and executes the command without a prompt argument.

Unknown delivery modes and invalid variable names raise `ValueError`. The
function does not change directories, source environment files, or interpolate
the prompt.

`with_context` returns the original prompt for blank context; otherwise it
joins context and prompt with two newline characters.

## Unary parser protocol and registry

`OutputParser` requires `streaming: bool` and async
`consume(client, sandbox, script, rpc_timeout, step_manager) -> ParseResult`.
`ParseResult` contains `text`, `ok`, optional dictionary `raw`, and
`error_message`.

The registry loads built-ins into an in-memory dictionary. On a miss it scans
the `nat.orchestrator.output_parsers` entry-point group, instantiates a loaded
class when needed, verifies the runtime protocol, caches it, and returns it.
Unknown names raise `KeyError` including the known parser names.

`RawParser` calls collected OpenShell `exec` using `bash` with the generated
script as stdin. Exit zero returns stdout verbatim. Non-zero exit returns
stderr or an `exit_code=N` fallback.

`ClaudeJsonParser` performs the same execution and walks stdout lines backward
for the last valid JSON object. It succeeds only when that object exists, the
process exits zero, and `is_error` is false. On success it returns the
envelope's `result` and retains the full object in `raw`. On failure it returns
up to 500 characters of stripped stderr, then trailing stdout, then an exit
code fallback.

## Lifecycle, state, and concurrency

Each NAT builder closure owns at most one OpenShell client. Client creation is
lazy and guarded by a per-builder `asyncio.Lock` with a second check inside the
critical section, so concurrent first invocations share one client. Teardown is
deterministic.

Parser instances are cached globally. Implementations registered by third
parties must therefore be safe for reuse across invocations and workflows.

## Verification

The unit suite runs without NAT or a live OpenShell gateway because it imports
only the transport-neutral core and parsers and uses a fake collected-exec
client. It verifies:

- static-context composition;
- shell quoting for command, arguments, and prompt;
- collision-free heredoc selection;
- defensive prompt-delivery validation;
- raw parser success/failure and OpenShell call shape;
- Claude trailing-envelope selection and malformed output failure; and
- built-in parser discovery and unknown-name errors;
- strict configuration and Claude wrapper defaults;
- lazy client construction, concurrent first-call sharing, and teardown; and
- missing streaming terminal-event reporting.

A real NAT loader/discovery smoke and full stream event matrix still require an
environment with `nvidia-nat-core` and `h-openshell` installed.

## Disagreements and remaining baseline work

1. The HLD lists four predecessor functions as candidates; they are not
   implemented and are not registered by this package.
2. The root `h-nat` README's invoke/stream summary matches the implemented
   first slice. Redis-backed cycles described in the HLD remain target scope.
3. The predecessor generic stdin path used a fixed heredoc delimiter. Current
   code selects a collision-free delimiter, closing a prompt-integrity edge
   case rather than preserving the predecessor behavior.
4. The predecessor used package/import names beginning `h-network-` and
   `nat.plugins.orchestrator`. Current code uses the public distribution name
   `h-orchestrator` and namespace `nat.plugins.h_orchestrator`.
5. The predecessor dedicated `claude_stream` performed eager gateway health
   checking. It has not been ported; all implemented clients initialize lazily.
6. Unary failure and h-agent stream failure remain error-as-data for baseline
   compatibility. The HLD calls for an explicit final failure contract before
   the public interface is declared stable.
7. The predecessor parser comments proposed streaming parser variants, but
   neither predecessor nor current code implements them. The current streaming
   path explicitly bypasses the unary registry.
