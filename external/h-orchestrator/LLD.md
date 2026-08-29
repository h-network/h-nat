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

The package implements generic unary and streaming execution, Claude unary and
stream-json wrappers, two unary parsers, and a dispatcher-agnostic
Redis-backed chat cycle.

The predecessor's `claude_via_hramp` and `h_claude_cycle` functions have not
been ported. No h-ramp dependency is declared.

## Package layout

```text
external/h-orchestrator/
├── HLD.md
├── LLD.md
├── README.md
├── pyproject.toml
├── src/nat/plugins/h_orchestrator/
│   ├── __init__.py
│   ├── chat_cycle.py
│   ├── claude_stream.py
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
3.11-3.13. Runtime dependencies are `h-memory`, `h-openshell`, Redis 7.1,
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

### `claude_stream`

`ClaudeStreamConfig` inherits the strict generic invocation fields and supplies
Claude stateless stream-json flags. Its builder creates one OpenShell client
lazily under an async lock and closes it at teardown.

The consumer uses an incremental UTF-8 decoder and retains incomplete text
between stdout chunks. Complete lines are decoded as JSON; non-JSON and
non-result events are ignored. The last successful `type="result"` text is
returned after exit zero. Error results, non-zero exits, a missing exit event,
or a missing successful result return documented bracketed error strings.
Stderr is logged with a 300-character bound.

### `h_chat_cycle`

`HChatCycleConfig` is strict. It requires a dispatcher name and accepts
optional `chat_id`, `pod`, and `agent` defaults, Redis URL, hot-tier count bound,
and turn TTL. `HChatCycleInput` contains a required message plus optional
per-request addressing and metadata. Per-request addressing wins over config;
all three axes are required after resolution. `pod` and `agent` use the
ADR-012-safe token pattern.

The builder lazily initializes one decoded Redis client and resolves the
configured NAT dispatcher under one async lock. Each invocation:

1. reads indexed h-memory payloads and orders live, valid JSON objects oldest
   first;
2. builds the predecessor-compatible history/current-message prompt;
3. calls `dispatcher.ainvoke(prompt, to_type=str)` and measures wall time;
4. on a raised dispatcher error, returns a typed error result without writes;
5. otherwise creates a per-request `BoundedBufferStore` and writes the user,
   then assistant, using the configured TTL and count bound; and
6. returns result text, resolved chat ID, prior count, duration, and assistant
   turn key.

String converters accept either a JSON typed request or a bare message and
reduce typed output to its result text. The Redis client closes at teardown.

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

The chat-cycle closure similarly guards Redis-client construction and NAT
dispatcher lookup with one lock. `BoundedBufferStore` is per invocation so
resolved tenant axes can vary by request.

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
- missing streaming terminal-event reporting;
- Claude stream decoding across split UTF-8 and JSON chunks;
- Claude stream error-result handling; and
- chat addressing, prompt shape, chronological reads, and missing-axis errors.

A real NAT loader/discovery smoke and full stream event matrix still require an
environment with `nvidia-nat-core` and `h-openshell` installed.

## Disagreements and remaining baseline work

1. h-ramp support is deferred. It is not one of the five planned `h-nat`
   modules and has no public package/import contract here. `claude_via_hramp`
   and `h_claude_cycle` require a decision on whether h-ramp becomes a sixth
   module before they can be implemented.
2. The root `h-nat` README summarizes only invoke/stream. This module now also
   implements the explicit `h_chat_cycle` memory composition.
3. The predecessor generic stdin path used a fixed heredoc delimiter. Current
   code selects a collision-free delimiter, closing a prompt-integrity edge
   case rather than preserving the predecessor behavior.
4. The predecessor used package/import names beginning `h-network-` and
   `nat.plugins.orchestrator`. Current code uses the public distribution name
   `h-orchestrator` and namespace `nat.plugins.h_orchestrator`.
5. The predecessor dedicated `claude_stream` performed eager gateway health
   checking. Current `claude_stream` initializes lazily and safely handles JSON
   or UTF-8 tokens split across transport chunks.
6. Unary failure and h-agent stream failure remain error-as-data for baseline
   compatibility. The HLD calls for an explicit final failure contract before
   the public interface is declared stable.
7. The predecessor parser comments proposed streaming parser variants, but
   neither predecessor nor current code implements them. The current streaming
   path explicitly bypasses the unary registry.
