# h-openshell

`h-openshell` is the h-nat async client and NeMo Agent Toolkit integration for
the public NVIDIA OpenShell gateway API.

The first implementation slice provides the installable Python client,
gateway-home/mTLS discovery, sandbox lifecycle operations, and collected or
streaming command execution. NAT component registration is the next slice and
is not yet shipped from this branch.

```python
from nat.plugins.h_openshell import OpenShellClient

async with OpenShellClient.from_default_home() as gateway:
    status, version = await gateway.health()
    sandbox = await gateway.create_sandbox("agent-1")
    result = await gateway.exec(sandbox.name, ["printf", "hello"])
    print(result.stdout_text)
```

The package vendors protobuf source and generated Python stubs pinned to
OpenShell v0.0.36. A normal wheel install does not require `protoc` or a
post-install stub-generation script.

See [HLD.md](HLD.md) for the module boundary and [LLD.md](LLD.md) for the
implementation details that must evolve with the code.
