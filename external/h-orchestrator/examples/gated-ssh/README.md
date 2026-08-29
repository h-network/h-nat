# Gated generic SSH execution

This workflow executes a command directly from the NAT process over SSH. It is
vendor-neutral: the target host and command are request parameters. Before any
connection opens, `h_ssh_exec` submits the exact target, port, and command to
the configured `h_asimov_gate`; only an `ALLOW` verdict permits execution.

Install the local `h-asimov` and `h-orchestrator` packages, then configure the
judge and SSH deployment credentials:

```bash
export H_NAT_LLM_MODEL=your-model
export H_NAT_LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
export OPENAI_API_KEY=...
export SSH_USERNAME=operator
export SSH_PASSWORD=...
export SSH_KNOWN_HOSTS=/etc/ssh/ssh_known_hosts
```

Never place SSH credentials in the input JSON. They are deployment config and
are absent from the tool schema and gate prompt.

Validate and invoke the workflow:

```bash
nat validate --config_file external/h-orchestrator/examples/gated-ssh/workflow.yaml
nat run --config_file external/h-orchestrator/examples/gated-ssh/workflow.yaml \
  --input '{"host":"router.example","command":"show version"}'
```

Replace the inline ground rules with deployment-specific rules and a precise
denylist before using this against live infrastructure. Host-key verification
is enabled by default. Keep it enabled and provision `SSH_KNOWN_HOSTS`; setting
`verify_host_key: false` removes server identity verification and is intended
only for controlled test environments.
