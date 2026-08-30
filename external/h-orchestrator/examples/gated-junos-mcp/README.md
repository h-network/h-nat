# Gated Junos MCP tools

This example connects NAT to the deployed `junos-mcp-server` while preventing
an agent from reaching execution-capable tools without authorization.

The raw `mcp_client` group uses an explicit `include` allowlist containing only
four inspection tools:

- `gather_device_facts`
- `get_junos_config`
- `get_router_list`
- `junos_config_diff`

Five other MCP members are deliberately absent from that list and therefore do
not enter NAT's public function registry. Each is exposed through a separate
`h_gated_mcp_tool` function which preserves the MCP server's live input schema,
submits the exact tool name and validated arguments to `h_asimov_gate`, and
calls the hidden raw member only for `ALLOW`.

Configure the endpoint credential and OpenAI-compatible judge/agent model:

```bash
export JUNOS_MCP_TOKEN=...
export H_NAT_LLM_MODEL=your-model
export H_NAT_LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
export OPENAI_API_KEY=...

nat validate --config_file external/h-orchestrator/examples/gated-junos-mcp/workflow.yaml
nat run --config_file external/h-orchestrator/examples/gated-junos-mcp/workflow.yaml \
  --input "List the available routers"
```

Do not add an execution-capable raw member to `junos_mcp.include`. The wrapper
will refuse to build if its raw member is publicly included, but the allowlist
should still be reviewed whenever the MCP server adds or renames tools.
