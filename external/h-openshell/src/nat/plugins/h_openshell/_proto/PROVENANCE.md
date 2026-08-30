# OpenShell protobuf provenance

These protobuf definitions are vendored verbatim from NVIDIA/OpenShell tag
`v0.0.116`, commit `d1155aa70042d3e2ee49dbfa15346b108b7c1d92`:

<https://github.com/NVIDIA/OpenShell/tree/v0.0.116/proto>

Vendored source files used by h-openshell:

- `options.proto`
- `datamodel.proto`
- `sandbox.proto`
- `openshell.proto`

The adjacent Python files were generated with `grpcio-tools==1.60.1` and then
mechanically changed only to use package-relative imports for other generated
modules in this directory.
