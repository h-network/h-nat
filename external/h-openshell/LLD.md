# h-openshell low-level design

## Document contract

This is a living description of the implementation in
`external/h-openshell/`. It is not a one-time implementation proposal. Any
change to package layout, configuration, wire compatibility, public types,
gateway calls, NAT registration, lifecycle behavior, or verification must
update this document in the same branch.

`HLD.md` owns the durable module boundary and public intent. This document owns
the concrete mechanisms used to realize it.

## Current repository state

`external/h-openshell/` is an installable `h-openshell` distribution with
stable domain values, typed module errors, gateway-home/mTLS discovery, a
reusable async gRPC client, pinned protobuf source, vendored generated stubs,
and unit tests. Seven canonical `h_openshell_*` functions are registered
through the `nat.components` entry point. The wheel imports without `protoc` or
a post-install generation step.

## Implementation status

| Area | Current state | Maintenance rule |
|---|---|---|
| Packaging | `h-openshell` 0.1.0.dev0; Python 3.11–3.13; grpcio, protobuf, and nvidia-nat runtime | keep release metadata and supported runtimes aligned |
| Gateway client | implemented in `client.py` | extend only with tests and same-branch LLD updates |
| Wire schema | OpenShell v0.0.116 (`d1155aa`) sources and generated stubs vendored in `_proto/` | keep aligned with the supported gateway release |
| NAT functions | seven functions implemented in `register.py` | keep names, inputs, and outputs aligned with the registered components |
| NAT resource | absent and not contracted | document only if a resource is introduced |
| Verification | 31 pytest cases, NAT discovery/workflow-build checks, clean-wheel import, and live v0.0.116 create/get/list/exec/delete validation | preserve automated coverage and repeat compatible-gateway validation for wire or lifecycle changes |

## Package shape

The implementation keeps transport, domain values, and NAT adaptation
separate:

```text
external/h-openshell/
├── HLD.md
├── LLD.md
├── README.md                  operator-facing usage
├── pyproject.toml             package metadata, dependencies, NAT entry point
├── src/nat/plugins/h_openshell/
│   ├── __init__.py            intentionally exported Python API
│   ├── client.py              async gateway adapter
│   ├── models.py              stable Sandbox and ExecResult values
│   ├── errors.py              public module exception hierarchy
│   ├── register.py            thin NAT configs and builders
│   └── _proto/                pinned schema and generated stubs
└── tests/
    ├── test_client.py         fake-stub client behavior
    └── test_register.py       NAT discovery/build/output behavior
```

Generated modules are private implementation details. `__init__.py` should
export only the supported Python API so callers do not depend on protobuf
classes accidentally.

## Client mechanics

### Construction

`client.py` defines `OpenShellClient`, which owns one `grpc.aio.Channel` and its
generated gateway stub. It supports explicit construction from endpoint and
certificate bytes for tests and advanced callers, plus `from_default_home`,
which resolves:

1. the OpenShell home from an explicit path, then `OPENSHELL_HOME`, then the
   platform default;
2. the active gateway name;
3. its gateway endpoint from metadata, unless explicitly overridden; and
4. `ca.crt`, `tls.crt`, and `tls.key` from that gateway's mTLS directory.

The channel uses `grpc.ssl_channel_credentials`. A server-name override is a
channel option for matching the certificate identity; it is not an insecure
mode. Construction failures identify the missing or invalid configuration path
without including secret file contents.

The client implements `__aenter__`, `__aexit__`, and idempotent asynchronous
close behavior. Constructor-only `channel_factory` and `stub_factory` injection
points keep transport tests deterministic without widening the exported API.

### Stable values

`models.py` defines frozen, slotted dataclasses. Gateway protobuf messages are
converted at the boundary:

- `Sandbox` includes gateway ID, consumer-facing name, workspace, numeric
  phase, and symbolic phase name. These map from `Sandbox.metadata` and
  `Sandbox.status`; v0.0.116 removed the former public `namespace` field.
- `ExecResult` includes exit code and raw stdout/stderr bytes, with replacement-
  decoding convenience properties for text consumers.

NAT functions serialize these values explicitly. They do not serialize
protobuf objects or dataclass internals implicitly.

### Sandbox identity

Names are canonical consumer state. When an RPC requires a UUID, the client
resolves a supplied name immediately before the RPC. It does not cache the
mapping: deletion followed by creation with the same name can produce a new
UUID. A syntactically valid UUID may take a direct fast path where the gateway
RPC accepts it.

When an RPC is name-keyed and the caller supplies a UUID, `_resolve_name`
requests offset-based pages of 100 until it finds the UUID or exhausts results.
It also stops if a gateway repeats only already-seen IDs, preventing an
infinite loop if offset is ignored.

### Lifecycle calls

The first-release client maps to the public gateway API as follows:

| Client operation | Gateway behavior |
|---|---|
| `health` | unary health request with a short configurable deadline |
| `get_sandbox` | fetch by name and convert to `Sandbox` |
| `list_sandboxes` | list with explicit limit/pagination semantics |
| `create_sandbox` | create by name/spec, then reconcile until ready |
| `delete_sandbox` | delete by name, optionally reconcile until absent |
| `exec_stream` | resolve required identity, send exec request, yield events |
| `exec` | consume `exec_stream`, preserving stdout, stderr, and exit code |

`create_sandbox` first validates a non-empty requested name against the
v0.0.116 routable-name contract: at most 19 UTF-8 bytes, lowercase ASCII
letters/digits/hyphens only, no leading or trailing hyphen, and no consecutive
hyphens. Empty remains valid and asks the gateway to generate a name. It then
checks paginated results for an existing sandbox with the same name. Ready returns
success; an in-progress phase is watched or polled until ready; an error phase
raises `SandboxLifecycleError`; deadline expiry raises `SandboxTimeoutError`
with the last observed phase. This slice polls `GetSandbox`; adopting
`WatchSandbox` requires compatible-gateway verification and an LLD update.

Every create request includes a present `SandboxSpec` message. When the caller
does not supply one, the client sends an empty-but-present `SandboxSpec()` so
the v0.0.116 gateway can apply its defaults; omitting the message is rejected
by the gateway as `INVALID_ARGUMENT: spec is required`.

`delete_sandbox` with waiting enabled succeeds only when absence is confirmed.
gRPC `NOT_FOUND` is the expected terminal state; unrelated RPC failures are
wrapped in `GatewayRPCError` with their status available through
`status_code`. Timeout raises `SandboxTimeoutError` even when the initial
delete response was positive.

### Execution calls

`exec_stream` yields pinned gateway `ExecSandboxEvent` objects without buffering
the complete output. It does not intercept `asyncio.CancelledError`, allowing
async-generator cancellation to propagate into the underlying gRPC iterator.

`exec` consumes that stream, appends stdout and stderr bytes in
event order within their respective streams, and records the terminal exit
code. `ExecResult.exit_code` is `None` when no terminal exit event arrives, so
missing terminal data is distinguishable from exit code zero.

No automatic execution retry is permitted in the initial implementation.

## NAT registration

`pyproject.toml` registers the `h_openshell` `nat.components` entry point to
`nat.plugins.h_openshell.register`. Config types use the canonical
`h_openshell_*` names listed in `HLD.md`.

All function configs share validated connection fields:

- `gateway_home` (optional path);
- `endpoint` (optional `host:port` override);
- `target_override` (TLS certificate name, default chosen to match supported
  OpenShell gateway certificates); and
- operation-specific RPC or lifecycle deadlines.

Builders create a client and close it in `finally` around the yielded NAT
function. All seven builders are network-lazy: channel construction reads local
configuration but no health or operation RPC occurs during workflow build. A
NAT `WorkflowBuilder` test builds all seven against an unreachable endpoint.

Unary functions return compact deterministic JSON with sorted keys. Sandbox
objects contain `id`, `name`, `workspace`, `phase`, and `phase_name`.
`h_openshell_exec` returns `exit_code`, `stdout`, and `stderr` together.
`h_openshell_exec_stream` emits one newline-delimited JSON object per gateway
event, with `type` equal to `stdout`, `stderr`, or `exit`. This preserves
incremental output and makes non-zero exit distinguishable from output text.

## Wire compatibility and generated code

`src/nat/plugins/h_openshell/_proto/` vendors `options.proto`,
`datamodel.proto`, `sandbox.proto`, and `openshell.proto` verbatim from
OpenShell v0.0.116, commit
`d1155aa70042d3e2ee49dbfa15346b108b7c1d92`. `PROVENANCE.md` records the tag,
commit, and source URL. The package also vendors the eight
`_pb2.py`/`_pb2_grpc.py` files generated with `grpcio-tools==1.60.1`.
Generated intra-schema imports are mechanically rewritten to package-relative
imports.

This keeps the generated code compatible with the declared gRPC runtime range.
The protobuf runtime is `>=5.27.2,<8`, matching NAT 1.8's transitive Milvus
requirement while remaining forward-compatible with the older generated code.
A clean wheel imports without `protoc`. Runtime imports do not
mutate `sys.path`. Updating the schema or generator requires regenerating all
eight files, rerunning unit and wheel checks, and updating this section.

## Errors, deadlines, and logging

`errors.py` defines `OpenShellError`, `ConfigurationError`, `GatewayRPCError`,
`SandboxLifecycleError`, and `SandboxTimeoutError`. `GatewayRPCError` retains
the original exception as `cause` and exposes its gRPC status as
`status_code`. `SandboxTimeoutError` is also a standard `TimeoutError`.

Every network operation has a finite configurable deadline. Logs may include
gateway endpoint, operation, sandbox name, phase, status code, duration, and
version. Logs must not include certificate/key bytes, environment values,
stdin, or full command output by default.

## Verification invariants

The automated suite contains 31 pytest cases covering client behavior, wire
compatibility, NAT discovery, workflow construction, output framing, and the
invariants identified below. Clean-wheel validation covers import without a
local protobuf compiler. Release validation against OpenShell v0.0.116 has
exercised create, get, list, exec, and delete through the live gateway API.

1. gateway-home precedence and endpoint normalization;
2. mTLS channel creation with verification retained;
3. channel closure on normal teardown and builder failure;
4. protobuf-to-domain conversion, including unknown phase values;
5. no stale name-to-UUID cache after delete/recreate;
6. create idempotency, ready/error transitions, and timeout reporting;
7. delete confirmation only on `NOT_FOUND` and propagation of other errors;
8. streaming output is incremental and caller cancellation reaches gRPC;
9. collected exec preserves binary stdout/stderr and missing exit state;
10. all canonical `h_openshell_*` components are discoverable after wheel
    installation; and
11. a clean wheel imports and runs without a manual protobuf generation step.

If policy mutation is later added, its verification matrix must separately
cover successful dynamic activation and generation changes, invalidation of
affected connections, recreation-required static policy changes, and the
first-`protocol: tcp` infrastructure edge case described in `HLD.md`.

Integration tests must state the compatible OpenShell gateway version and be
opt-in when they require a live gateway or credentials.

## LLD update checklist

For every implementation change in this module:

- replace target descriptions with exact current filenames and symbols;
- update public config fields, defaults, JSON shapes, and exceptions;
- record the pinned OpenShell schema provenance and packaging behavior;
- add or update tests for each changed invariant;
- update `HLD.md` if the public boundary or first-release scope changes; and
- remove stale text rather than retaining historical behavior in this file.

History belongs in Git and release notes. This LLD describes the current tree.
