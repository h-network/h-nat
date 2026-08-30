# h-asimov adversarial benchmark

This measures `h_asimov_gate` against deliberate attempts to break it, not
just cases where it's expected to work. It reports two distinct failure
classes, not a single pass/fail number:

- **False negative** — a dangerous command was ALLOWed. Security failure.
- **False positive** — a legitimate, benign command was DENYed. Usability
  failure (and the one that erodes trust in the gate fastest in practice).

## Why two workflow variants

`workflows/generic-rules.yaml` uses a naive, domain-agnostic ground-rules
document (the same style as `examples/h-asimov/standalone-gate/`).
`workflows/device-rules.yaml` uses a ground-rules document written
specifically for network-device commands (Junos-style CLI), including
explicit guidance on read-only `show`/`monitor`/diagnostic commands and
explicit instructions to evaluate whole command chains and resist injection
and claimed-authorization framing.

Running the *same* case set against both answers a specific question: is a
given failure (especially the false-positive class) a structural limit of
the gate, or a fixable gap in how the ground rules are written? If
`device-rules.yaml` clears a false positive that `generic-rules.yaml` trips
on, that's a ground-rules authoring problem, not a gate-logic problem.

## Case categories (`cases.py`)

- **control** — a trivially benign and a trivially destructive case, as a
  harness sanity check. If either of these fails, something is broken in
  the benchmark setup, not in the gate.
- **encoding** — base64, hex, and Unicode-homoglyph obfuscation of a
  destructive command. Tests whether the LLM judge's semantic reading
  catches what the denylist's plain substring match structurally cannot.
- **injection** — direct "ignore previous instructions" and
  roleplay/jailbreak-persona framing wrapped around a destructive payload.
- **auth-mimicry** — a claimed approval, ticket number, or on-call
  authority attached to a destructive command.
- **split-batched** — a destructive clause buried inside an otherwise
  benign command chain.
- **false-positive** — legitimate, read-only, or explicitly-scoped commands
  that sound risky on the surface (`show system commit`, `request support
  information`, a scoped `delete` of a confirmed-safe temp file, etc.).
  `FP-COMMIT-LOG` is the exact case found in live testing.

## Running it

1. Install `h-asimov` (`pip install -e external/h-asimov`).
2. Copy `vars.example.yaml` to `vars.yaml` in this directory and fill in a
   real OpenAI-compatible endpoint. `vars.yaml` is gitignored — never commit
   it.
3. Run:
   ```bash
   python benchmark/h-asimov/run_bench.py
   ```

`run_bench.py` always exits 0 — it's a measurement tool, not a CI gate.
Read the summary, and especially the false-negative section at the end.

## Recording results

Real, authoritative results belong in `RESULTS.md`, captured from an actual
run against a real LLM endpoint in the operator's lab — that's a separate
follow-up from building this benchmark. `RESULTS.md` also carries a labeled
dev-verification sample confirming the harness itself runs end-to-end; that
sample is not the authoritative capture.
