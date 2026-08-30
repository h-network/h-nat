# Benchmarks

Adversarial/quality benchmarks, grouped by the module they exercise —
outside `external/` for the same reason `examples/` is: each
`external/<module>/` tree stays a pure installable NAT module, and a
benchmark is a repository-level evaluation asset, not something that ships
in a module's wheel.

Unlike `examples/`, which read endpoint config from shell environment
variables, each `benchmark/<module>/` directory carries its own
`vars.example.yaml` (placeholder values, committed) and expects a real
`vars.yaml` (gitignored) alongside it before `run_bench.py` is run — a
benchmark run is many invocations against one endpoint, so a single
committed-shape config file is less friction than exporting several env
vars by hand.

Run commands from the repository root so paths and editable module
installs resolve consistently, same as `examples/`.
