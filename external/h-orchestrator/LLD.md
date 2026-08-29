# h-orchestrator low-level design

## Canonical status and scope

This is the canonical low-level document for the code currently present in
`h-nat/external/h-orchestrator`.

At the time of writing, no `h-orchestrator` implementation has landed in
`h-nat`; the directory contains only `.gitkeep`. Therefore there are currently
no installed package, NAT registrations, runtime dependencies, configuration
models, or executable code in this repository.

The remainder of this document records the verified predecessor baseline in
`h-network-nemo-agent-toolkit/external/h-network-orchestrator`. It is the
starting point to be ported and refactored, not a claim about code already
available from `h-nat`. This document must be updated as implementation lands;
implemented behavior then supersedes the baseline notes below.

## Predecessor package baseline

The predecessor is a Python 3.11-3.13 setuptools package named
`h-network-orchestrator`. Its NAT component entry point is:

```toml
[project.entry-points."nat.components"]
orchestrator = "nat.plugins.orchestrator.register"
```

It depends on the predecessor OpenShell communicator, memory, and h-ramp
packages plus Redis. Its source package is
`nat.plugins.orchestrator`, arranged as follows:

```text
orchestrator/
├── register.py             # configs and primary NAT registrations
├── core.py                 # generic invoke/stream lifecycle
├── claude.py               # Claude stream-json helper
├── parsers/
│   ├── __init__.py         # built-ins and entry-point lookup
│   ├── raw.py
│   └── claude_json.py
├── wrappers/
│   └── claude.py           # claude_invoke specialization
├── _cycle_common.py        # history, addressing, and h-ramp helpers
├── chat_cycle.py           # generic memory-aware composite
├── claude_cycle.py         # Claude/h-ramp memory-aware composite
└── claude_via_hramp.py     # stateless h-ramp dispatcher
```

Importing `register.py` registers its local functions, then imports the wrapper
and composite modules for their registration side effects.

## Generic OpenShell invocation

### Configuration

`AgentInvokeConfig` is strict (`extra="forbid"`) and contains:

- gateway connection: `gateway_home`, `endpoint`, and
  `target_override="localhost"`;
- execution: required `sandbox` and `command`, `args=[]`, and
  `rpc_timeout_seconds=600.0`;
- prompt delivery: `prompt_via="arg"`, constrained to `arg`, `stdin`, or
  `env:VARNAME`;
- prompt composition: optional static `context`;
- output handling: `output_parser="raw"`.

`h_agent_stream` subclasses the same configuration. Although it accepts
`output_parser`, the predecessor streaming implementation does not consult it;
streaming is raw stdout only.

### Script construction

`build_script` emits a UTF-8 bash script beginning with `set -e`. Command,
arguments, and prompt values are quoted with `shlex.quote`. The prompt is then:

- appended as the final argument;
- placed in a single-quoted `__H_AGENT_PROMPT__` heredoc; or
- exported through the validated environment-variable name.

The core does not add a working-directory change or source an environment
file. Callers that require such behavior must encode it in their command and
arguments.

### Unary lifecycle

`agent_invoke_builder` resolves the configured parser at workflow-build time
but constructs `OpenShellClient` only on the first invocation. Each call:

1. prepends static context with a two-newline separator, when configured;
2. builds the bash script;
3. calls `parser.consume(...)` with the client, sandbox, script, timeout, and
   NAT intermediate-step manager;
4. returns `ParseResult.text` on success or `error_message` on failure.

The client is closed when NAT tears down the builder. There is no memory read
or write in this path.

### Streaming lifecycle

`agent_stream_builder` also creates its client lazily. It calls
`OpenShellClient.exec_stream`, yields decoded stdout payloads, logs stderr, and
records the terminal exit code. A non-zero or missing terminal status produces
a final `[exit_code=N]` chunk. The client is closed at teardown.

The dedicated predecessor `claude_stream` path is separate from this generic
builder. It uses Claude `--output-format stream-json`, consumes events through
`claude_invoke_stream`, and returns only the final successful result text. In
contrast to the generic builders, it constructs its OpenShell client and runs
`health()` while the workflow is built, then closes the client at teardown.

## Output parser contract

`OutputParser` is a runtime-checkable protocol with a `streaming: bool`
attribute and one async `consume(...) -> ParseResult` method. `ParseResult` is
an immutable dataclass containing `text`, `ok`, optional parser-specific `raw`
data, and `error_message`.

The parser registry resolves an in-memory built-in first, then lazily searches
the `nat.orchestrator.output_parsers` entry-point group and caches a matching
class or instance. Unknown names raise `KeyError` with the known built-ins.

- `RawParser` executes through `OpenShellClient.exec`; exit code zero returns
  stdout verbatim, while failure returns stderr or an exit-code fallback.
- `ClaudeJsonParser` executes the same way, walks stdout backward for the last
  JSON-object line, and succeeds only when the process exit is zero and the
  envelope does not set `is_error`. It returns the envelope's `result` value
  and retains the envelope in `ParseResult.raw`.

## Agent-specific wrappers and dispatchers

`ClaudeInvokeConfig` subclasses `AgentInvokeConfig` and supplies `command` as
`claude`, positional prompt delivery, the `claude_json` parser, and stateless
JSON-mode Claude flags. Optional `hook_settings_path` appends
`--settings <path>` to the configured arguments. Its NAT builder delegates
unchanged to `agent_invoke_builder`.

`claude_via_hramp` is a separate stateless `str -> str` dispatcher. Its strict
config requires `rampd_target`, `peer_id`, and `claude_model`, with timeout,
binary path, and flags configurable. A `FlockRouter` starts lazily on first
call. The synchronous router iterator is drained on a worker thread; stdout is
parsed as a trailing Claude JSON envelope. The router stops at teardown.

The h-ramp helper returns bracketed error strings for `RampError`, non-zero
exit, or a missing/error envelope. This error-as-data behavior is part of the
predecessor baseline and requires an explicit compatibility decision during
the public port.

## Memory-aware composites

The predecessor contains two typed composites:

- `h_chat_cycle` delegates the model call to any configured NAT function with
  a `str -> str` contract.
- `h_claude_cycle` dispatches Claude directly through h-ramp.

Both accept a message plus optional per-request `chat_id`, `pod`, `agent`, and
metadata. Addressing resolves per-request value first, then the configuration
default, and errors if any axis is missing. `pod` and `agent` are restricted to
alphanumeric, underscore, and hyphen tokens beginning with an alphanumeric
character.

Each invocation lazily creates a Redis client, reads prior turns from the
`<pod>:<agent>:chat-index:<chat_id>` sorted set and corresponding turn keys,
orders them oldest first, and builds this prompt shape:

```text
Previous conversation:
[role] content
...

Current message:
<new message>
```

After a successful dispatch, a per-request `BoundedBufferStore` writes the
user and assistant turns with the configured TTL and hot-tier count bound. The
typed output contains `result`, resolved `chat_id`, `prior_turn_count`,
`duration_ms`, and the assistant `turn_id`. String and OpenAI Chat Completions
converters adapt the typed input/output for CLI and API use; incoming chat
requests use only the last user message because Redis is the history source of
truth.

The two composites differ at dispatch:

- `h_chat_cycle` lazily resolves `builder.get_function(dispatcher)` and invokes
  it with the composed prompt. A raised dispatcher exception becomes a typed
  error result and is not persisted.
- `h_claude_cycle` lazily starts `FlockRouter`, uses the shared Claude h-ramp
  helper, and does not persist its own bracketed error results.

Redis clients and routers are closed or stopped during builder teardown.

## Concurrency and state

Infrastructure clients are held in mutable dictionaries captured by NAT
function closures and initialized on first use. The predecessor does not guard
initialization with a lock, so simultaneous first calls could construct more
than one client or router. This should be reviewed during the port. Per-request
addressing and `BoundedBufferStore` construction otherwise avoid binding one
composite instance to a single tenant.

## Disagreements and transition notes

This section records where current `h-nat` state, the intended HLD, and the
predecessor documentation differ.

1. **No current implementation:** the HLD describes the target architecture,
   while current `h-nat` contains only `.gitkeep`. None of the functions above
   are presently available from this repository.
2. **Names and package paths:** the predecessor package and imports use
   `h-network-*` distribution names and `nat.plugins.h_network_*` modules.
   Public `h-nat` names and import paths must be selected and implemented; they
   must not be inferred from this baseline document.
3. **README scope:** the root `h-nat` README summarizes `h-orchestrator` as
   invoking or streaming a coding-agent CLI. The predecessor additionally
   contains Redis-backed chat-cycle composites and an h-ramp transport path.
   Their inclusion in the public module remains a port decision.
4. **Predecessor README registration list:** it says the plugin adds three NAT
   functions and its layout omits newer modules, but the inspected predecessor
   code registers seven functions: `h_agent_invoke`, `h_agent_stream`,
   `claude_invoke`, `claude_stream`, `claude_via_hramp`, `h_chat_cycle`, and
   `h_claude_cycle`. This LLD follows code, not that stale summary.
5. **Memory boundary wording:** predecessor prose broadly says memory
   composition is the consumer's job. That is accurate for invoke/stream
   primitives, but the later `h_chat_cycle` and `h_claude_cycle` code composes
   Redis memory internally. The HLD and this LLD distinguish those layers.
6. **Streaming parser claims:** parser comments discuss future streaming parser
   variants, but the inspected registry contains only `raw` and `claude_json`,
   and `h_agent_stream` bypasses the parser registry. The LLD records the
   implemented behavior rather than the comments' planned behavior.
7. **Lazy-build consistency:** the generic invocation builders and composite
   clients initialize lazily, but the separate `claude_stream` registration
   constructs a client and checks gateway health at build time. The target HLD
   calls for lazy infrastructure initialization; the port must reconcile this
   exception.
