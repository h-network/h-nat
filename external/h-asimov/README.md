# h-asimov

NAT plugin: pre-flight LLM safety judge. Workflow-callable as
`_type: h_asimov_gate` from any agent toolkit YAML flow.

## Status

Implemented. Gate logic (`_internal/firewall.py`, `_internal/denylist.py`,
`_internal/asimov.py`, `_internal/noop.py`) is ported from
`h-network-asimov-firewall` (see `LLD.md` for the full port history —
what carried over unchanged, what was reworked, and what's newly
built). Unlike that predecessor, the NAT registration itself
(`register.py`) is implemented, not a stub.

## Layout

```
src/nat/plugins/h_asimov/
  register.py          # AsimovGateConfig, @register_function, FunctionInfo.from_fn
  __init__.py
  defaults/
    denylist.default.txt
    groundRules.default.md
  _internal/            # ported/reworked gate logic (see LLD.md §2, §4)
    firewall.py          # Decision pipeline (L1 + L2)
    denylist.py           # L1 fast denylist
    asimov.py              # L2 LLM judge, called through NAT's LLM abstraction
    noop.py                 # mode=noop variant for dev/test

tests/
  test_firewall.py
  test_denylist.py
  test_asimov.py
  test_noop_firewall.py
  test_register.py      # AsimovGateConfig validation + h_asimov_gate builder
  conftest.py

pyproject.toml
requirements.txt
requirements-test.txt
HLD.md                  # high-level design
LLD.md                  # canonical low-level design + discrepancy log
```

## Usage

```yaml
llms:
  judge_llm:
    _type: openai
    base_url: ...
    model_name: ...

functions:
  bgp_gate:
    _type: h_asimov_gate
    llm_name: judge_llm
    ground_rules: defaults/bgp.md
    # ground_rules_inline: "Allowed: ..."
    denylist: defaults/denylist.default.txt
    fail_open: false
```

Audited opt-out (no judge call, always ALLOW):

```yaml
functions:
  bgp_gate:
    _type: h_asimov_gate
    mode: noop
```

`h_asimov_gate` is a pure judge: it takes a `command: str` and returns
a typed `GateDecision` (verdict/layer/reason). It does not execute
anything — the caller's workflow decides what to do with the verdict.
