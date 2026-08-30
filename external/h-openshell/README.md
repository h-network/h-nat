# h-openshell

`h-openshell` is the h-nat async client and NeMo Agent Toolkit integration for
the public NVIDIA OpenShell gateway API.

The package provides an installable Python client, gateway-home/mTLS discovery,
sandbox lifecycle operations, collected or streaming command execution, and
seven NAT-discoverable functions.

```python
from nat.plugins.h_openshell import OpenShellClient

async with OpenShellClient.from_default_home() as gateway:
    status, version = await gateway.health()
    sandbox = await gateway.create_sandbox("agent-1")
    result = await gateway.exec(sandbox.name, ["printf", "hello"])
    print(result.stdout_text)
```

The package vendors protobuf source and generated Python stubs pinned to
OpenShell v0.0.116. A normal wheel install does not require `protoc` or a
post-install stub-generation script.

## NAT functions

| `_type` | Result |
|---|---|
| `h_openshell_health` | gateway endpoint/status/version JSON |
| `h_openshell_create_sandbox` | ready sandbox JSON |
| `h_openshell_delete_sandbox` | deletion JSON |
| `h_openshell_get_sandbox` | sandbox JSON |
| `h_openshell_list_sandboxes` | sandbox JSON array |
| `h_openshell_exec` | exit code/stdout/stderr JSON |
| `h_openshell_exec_stream` | newline-delimited stdout/stderr/exit JSON events |

Connection settings are shared: `gateway_home`, `endpoint`, and
`target_override`. Exec functions also require `sandbox` (name or UUID). Every
builder is network-lazy, so declaring a function does not require the gateway
to be reachable during workflow construction.

```yaml
workflow:
  _type: h_openshell_exec
  sandbox: agent-1
  # gateway_home: /custom/openshell/home
  # endpoint: remote-gateway.example:8080
  # target_override: localhost
```

See [HLD.md](HLD.md) for the module boundary and [LLD.md](LLD.md) for the
implementation details that must evolve with the code.
