# h-openshell high-level design

## Purpose

`h-openshell` is the h-nat integration for NVIDIA OpenShell. It gives Python
code and NeMo Agent Toolkit (NAT) workflows an asynchronous, authenticated
interface to an OpenShell gateway without invoking the `openshell` CLI as a
subprocess.

The module owns the boundary between h-nat and the gateway's public gRPC API:

- discover and authenticate to a gateway;
- inspect and manage sandbox lifecycle;
- execute commands in a sandbox, either collected or streamed; and
- translate gateway messages into small, stable Python and NAT-facing values.

OpenShell remains responsible for provisioning, isolation, policy enforcement,
and sandbox-internal supervisor communication. `h-openshell` is a gateway
client, not a compute driver, sandbox supervisor, policy engine, or general
workflow orchestrator.

## Place in h-nat

The five h-nat plugins are independently installable and compose at the NAT
workflow boundary. `h-openshell` supplies sandbox lifecycle and the direct
gateway execution transport. Higher-level modules may use that capability but
do not move their own concerns into this module:

```text
NAT workflow / Python consumer
             |
             v
        h-openshell
  NAT functions + Python client
             |
       authenticated gRPC
             |
             v
     OpenShell public gateway API
             |
             v
 sandbox provisioning, isolation, policy, supervisor
```

- `h-orchestrator` decides which agent CLI to invoke and how to parse it.
- `h-memory` and `h-recall` own conversational and semantic state.
- `h-asimov` decides whether a proposed action is allowed.
- the workflow or application composes those plugins; `h-openshell` does not
  embed agent, memory, recall, or safety policy logic.

Consumers that use a separate execution transport can still use
`h-openshell` only for lifecycle operations. Conversely, direct gateway exec is
a supported transport for consumers that do not need another transport layer.

## Public interface

The first public release is designed around two equivalent entry points: a
typed asynchronous Python client and NAT-discoverable components. The Python
client is the canonical gateway adapter; NAT registrations are thin lifecycle
and serialization adapters over it.

### Python API

The intended public Python surface is:

- `OpenShellClient`: an async, reusable gateway connection and async context
  manager;
- `Sandbox`: a stable view of gateway sandbox identity, workspace, and phase;
- `ExecResult`: collected exit code, stdout, and stderr;
- gateway health;
- create, get, list, and delete sandbox operations;
- collected command execution; and
- asynchronous streaming command execution.

Sandbox names are the consumer-facing identity. Gateway UUIDs may be accepted
where useful, but UUID resolution is an adapter concern and must not leak into
workflow state as the primary identity. Name-to-UUID mappings must not be
cached across delete/recreate cycles.

Explicitly requested names follow the gateway's DNS-routable contract: at most
19 UTF-8 bytes, lowercase ASCII letters/digits/hyphens only, no leading or
trailing hyphen, and no consecutive hyphens. An empty create name delegates
name generation to the gateway.

### NAT API

The initial NAT function family uses the h-nat namespace required by the
repository naming convention:

| NAT `_type` | Input | Output |
|---|---|---|
| `h_openshell_health` | ignored string | gateway status and version |
| `h_openshell_create_sandbox` | sandbox name | sandbox JSON |
| `h_openshell_delete_sandbox` | sandbox name or UUID | deletion JSON |
| `h_openshell_get_sandbox` | sandbox name or UUID | sandbox JSON |
| `h_openshell_list_sandboxes` | ignored string | JSON array of sandboxes |
| `h_openshell_exec` | command string | collected command result |
| `h_openshell_exec_stream` | command string | streamed command output |

Connection configuration is workflow configuration, not function input. It
includes an optional OpenShell home, an optional endpoint override for remote
gateways, and a TLS server-name override when the endpoint name differs from
the certificate identity. Exec functions additionally bind a sandbox.

A connected NAT resource may be added when a concrete in-repository consumer
needs to share one client across components. Until that interface is designed
and implemented, it is not part of the public contract.

## Connection and trust model

By default the client follows the same active-gateway configuration used by the
OpenShell CLI. It reads the active gateway's metadata and mTLS certificate
bundle from the OpenShell configuration home, constructs a secure `grpc.aio`
channel, and authenticates as an external gateway client.

The client may use an endpoint override to reach that gateway from another
host. The TLS server-name override controls certificate verification only; it
must not disable verification. Certificate and private-key bytes are never
returned through NAT functions or written to logs.

The persistent `ConnectSupervisor` control stream and `RelayStream` byte
channels are sandbox-identity protocols. They are outside this external
orchestrator interface and must not be impersonated by `h-openshell`.

## Lifecycle and execution semantics

- Create is idempotent by sandbox name: an existing sandbox is reconciled
  rather than duplicated.
- A successful create returns only after the sandbox is ready, unless a future
  API explicitly exposes asynchronous provisioning.
- Delete can optionally wait until the gateway reports the sandbox absent.
- Gateway error states, deadlines, and non-`NOT_FOUND` RPC errors are surfaced;
  they are not converted into false success.
- Collected exec preserves stdout, stderr, and exit code separately in Python.
- Streaming exec forwards output incrementally and remains cancellable by its
  caller.
- The module does not silently retry commands because execution may have side
  effects. Any retry contract must be explicit and operation-specific.

## Policy boundary

OpenShell policy is gateway-owned. A future policy surface in `h-openshell`
must preserve upstream semantics, including the distinction between
creation-locked policy sections and hot-reloadable network policy or middleware
sections. Successful dynamic-policy activation changes the policy generation
and invalidates affected connections; those effects must remain visible to the
caller. Changing `filesystem_policy`, Landlock, or process policy requires
sandbox recreation. Introducing the first `protocol: tcp` endpoint also
requires recreation when the sandbox did not start with TCP capture
infrastructure. Validation failures must be surfaced rather than simulated as
local success.

Policy mutation is not in the first-release interface above. Adding it requires
an HLD update, an LLD update describing the exact gateway RPC mapping, and tests
for activation, generation, recreation, and connection-invalidation cases.

## Compatibility and evolution

The gateway wire schema and generated Python stubs are one compatibility unit.
The implementation must pin them to a verified OpenShell release or commit and
record that provenance in-tree. Generated stubs must be available after a
normal package installation; consumers must not need an undocumented manual
generation step.

Compatibility changes that alter protobuf field layout, NAT `_type` names,
serialized JSON, lifecycle semantics, or exceptions are public-interface
changes. They require documentation and migration notes in the same branch as
the code.

## Non-goals for the first release

- implementing the OpenShell internal `ComputeDriver` contract;
- acting as a sandbox supervisor or relay endpoint;
- embedding agent-specific command construction or output parsing;
- owning conversational memory, semantic recall, or safety decisions;
- managing providers, SSH sessions, or the full policy API; and
- hiding gateway incompatibility behind best-effort protobuf decoding.

## Quality attributes

- **Security:** mTLS verification is mandatory and secrets are not logged.
- **Correctness:** public values and lifecycle results reflect gateway state.
- **Composability:** all blocking gateway work is exposed asynchronously.
- **Operability:** deadlines are configurable and errors retain useful context.
- **Portability:** local and remote gateways use the same client surface.
- **Maintainability:** `LLD.md` describes the code that is actually present and
  changes in the same branch whenever implementation behavior changes.
