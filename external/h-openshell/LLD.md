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

As of 2026-08-29, `external/h-openshell/` contains design documentation only.
There is no installable package, Python module, generated protobuf code, NAT
entry point, or test suite in this directory yet. Consequently, none of the
interfaces described as intended in `HLD.md` are currently implemented by the
public `h-openshell` module.

The earlier `h-network-openshell-communicator` repository is a behavioral
reference, not code shipped from this directory. Its behavior must be reviewed,
ported deliberately, renamed to the public h-nat conventions, and tested; it
must not be treated as implicitly present here.

## Implementation status

| Area | Current state | Required next update to this LLD |
|---|---|---|
| Packaging | absent | record distribution name, Python package, dependencies, and entry point |
| Gateway client | absent | record constructor, channel ownership, public methods, and errors |
| Wire schema | absent | record exact upstream version/commit, source URLs, and stub strategy |
| NAT functions | absent | record registered config classes, `_type` names, inputs, and outputs |
| NAT resource | absent and not contracted | document only if a resource is actually introduced |
| Verification | absent | map tests to the invariants below as tests land |

The sections below are the implementation constraints for the first code
change. They are labelled **target shape** until matching files exist. When a
piece lands, replace the target wording with exact filenames and behavior.

## Target package shape

The initial implementation should keep transport, domain values, and NAT
adaptation separate. Exact filenames remain a code-review decision, but the
responsibilities are:

```text
external/h-openshell/
├── HLD.md
├── LLD.md
├── README.md                  operator-facing usage
├── pyproject.toml             package metadata and NAT entry point
├── src/.../h_openshell/
│   ├── __init__.py            intentionally exported Python API
│   ├── client.py              async gateway adapter
│   ├── models.py              stable Sandbox and ExecResult values
│   ├── register.py            thin NAT configs and builders
│   └── _proto/                pinned schema and/or generated stubs
└── tests/
    ├── unit/                  fake-stub behavior and serialization
    └── integration/           opt-in tests against a compatible gateway
```

Generated modules are private implementation details. `__init__.py` should
export only the supported Python API so callers do not depend on protobuf
classes accidentally.

## Target client mechanics

### Construction

`OpenShellClient` owns one `grpc.aio.Channel` and its generated gateway stub.
It supports explicit construction from endpoint and certificate bytes for
tests and advanced callers, plus a convenience constructor that resolves:

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
close behavior. NAT builders own and close the clients they create.

### Stable values

Gateway protobuf messages are converted at the boundary:

- `Sandbox` includes gateway ID, consumer-facing name, namespace, numeric
  phase, and symbolic phase name.
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

When an RPC is name-keyed and the caller supplies a UUID, resolution must use a
gateway lookup with defined pagination behavior. A first implementation that
scans only one fixed-size list page must document that limitation or reject
UUID input rather than claiming exhaustive resolution.

### Lifecycle calls

The target first-release client maps to the public gateway API as follows:

| Client operation | Gateway behavior |
|---|---|
| `health` | unary health request with a short configurable deadline |
| `get_sandbox` | fetch by name and convert to `Sandbox` |
| `list_sandboxes` | list with explicit limit/pagination semantics |
| `create_sandbox` | create by name/spec, then reconcile until ready |
| `delete_sandbox` | delete by name, optionally reconcile until absent |
| `exec_stream` | resolve required identity, send exec request, yield events |
| `exec` | consume `exec_stream`, preserving stdout, stderr, and exit code |

Create first checks for an existing sandbox with the same name. Ready returns
success; an in-progress phase is watched or polled until ready; an error phase
raises a typed module error; deadline expiry raises a timeout that includes the
last observed phase. The implementation may prefer `WatchSandbox` over polling
when verified against the pinned gateway, but the chosen mechanism must be
recorded here when code lands.

Delete with waiting enabled succeeds only when absence is confirmed. gRPC
`NOT_FOUND` is the expected terminal state; unrelated RPC failures propagate.
A timeout must not be reported as confirmed deletion merely because the initial
delete response was positive.

### Execution calls

The Python streaming surface yields gateway events or a documented stable event
type without buffering the complete output. Cancellation by the async consumer
must cancel or close the underlying gRPC call promptly.

The collected surface consumes that stream, appends stdout and stderr bytes in
event order within their respective streams, and records the terminal exit
code. Missing terminal exit data is distinguishable from exit code zero.

No automatic execution retry is permitted in the initial implementation.

## Target NAT registration

The distribution registers one `nat.components` entry point. Config types use
the `h_openshell_*` names listed in `HLD.md`; predecessor bare
`openshell_*` names are not the canonical public names.

All function configs share validated connection fields:

- `gateway_home` (optional path);
- `endpoint` (optional `host:port` override);
- `target_override` (TLS certificate name, default chosen to match supported
  OpenShell gateway certificates); and
- operation-specific RPC or lifecycle deadlines.

Builders create a client and close it in `finally` around the yielded NAT
function. Whether a builder performs an eager health check is part of its
observable startup behavior and must be consistent:

- health and lifecycle functions should build without contacting the gateway,
  so a transiently unavailable gateway does not prevent workflow construction;
- exec functions may fail fast during build only if that behavior is documented
  and verified as the intended NAT operator experience.

Lifecycle functions return versioned, deterministic JSON shapes until NAT
offers an agreed structured-output contract. Streaming exec yields stdout
incrementally, reports stderr through a documented channel, and represents
non-zero exit without making it indistinguishable from ordinary stdout.

## Wire compatibility and generated code

Before the first client code lands, the implementation must select and record
an exact OpenShell release or commit whose protobuf layout matches the tested
gateway. Each vendored `.proto` file must carry source provenance. Using newer
schemas merely because they are on upstream `main` is unsafe when gateway
response layouts differ.

The packaging choice must satisfy a clean-wheel test. Acceptable strategies are
vendored generated stubs or deterministic generation as part of the build;
requiring users to run a post-install shell script is not the public-release
target. Runtime imports must not mutate global `sys.path` unless the final stub
layout makes that unavoidable and the trade-off is recorded here.

## Errors, deadlines, and logging

The implementation should define a small module exception hierarchy that
preserves the originating gRPC status and operation context. At minimum callers
must be able to distinguish configuration/authentication failure, gateway RPC
failure, lifecycle error phase, and lifecycle deadline expiry.

Every network operation has a finite configurable deadline. Logs may include
gateway endpoint, operation, sandbox name, phase, status code, duration, and
version. Logs must not include certificate/key bytes, environment values,
stdin, or full command output by default.

## Verification invariants

Tests added with the implementation must lock at least these behaviors:

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
