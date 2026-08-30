"""Schema-preserving, h-asimov-gated access to hidden MCP group tools."""

# Do not postpone annotations. NAT inspects nested callable annotations.

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nat.plugin_api import (
    Builder,
    FunctionBaseConfig,
    FunctionGroup,
    FunctionGroupRef,
    FunctionInfo,
    FunctionRef,
    register_function,
)

logger = logging.getLogger(__name__)


class GatedMcpToolConfig(FunctionBaseConfig, name="h_gated_mcp_tool"):
    """Reference one non-public member of an MCP function group."""

    model_config = ConfigDict(extra="forbid")

    mcp_group: FunctionGroupRef
    gate_fn: FunctionRef
    mcp_tool_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")


class GatedMcpResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "denied"]
    output: str = ""
    reason: str | None = None
    gate_layer: str | None = None


def _response_to_str(response: GatedMcpResponse) -> str:
    return response.model_dump_json()


def _gate_subject(tool_name: str, request: BaseModel) -> str:
    """Serialize the exact validated MCP operation presented for authorization."""

    return json.dumps(
        {
            "action": "mcp_tool_call",
            "tool": tool_name,
            "arguments": request.model_dump(exclude_none=True, mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@register_function(config_type=GatedMcpToolConfig)
async def h_gated_mcp_tool(config: GatedMcpToolConfig, builder: Builder):
    """Build a gated wrapper around one deliberately hidden MCP group member."""

    group = await builder.get_function_group(config.mcp_group)
    group_config = group.get_config()
    if not group_config.include:
        raise ValueError(
            "h_gated_mcp_tool requires the source group to use a non-empty include allowlist"
        )
    if config.mcp_tool_name in group_config.include:
        raise ValueError(
            f"raw MCP tool {config.mcp_tool_name!r} is publicly included; remove it from include"
        )

    full_name = f"{config.mcp_group}{FunctionGroup.SEPARATOR}{config.mcp_tool_name}"
    all_functions = await group.get_all_functions()
    if full_name not in all_functions:
        available = sorted(FunctionGroup.decompose(name)[1] for name in all_functions)
        raise ValueError(
            f"MCP tool {config.mcp_tool_name!r} not found in group {config.mcp_group!s}; "
            f"available tools: {available}"
        )
    raw_function = all_functions[full_name]
    input_schema = raw_function.input_schema
    if not isinstance(input_schema, type) or not issubclass(input_schema, BaseModel):
        raise TypeError(f"MCP tool {config.mcp_tool_name!r} has no Pydantic input schema")

    gate = await builder.get_function(config.gate_fn)

    def string_to_request(value: str) -> input_schema:
        return input_schema.model_validate_json(value)

    async def invoke(request: input_schema) -> GatedMcpResponse:
        decision = await gate.ainvoke(_gate_subject(config.mcp_tool_name, request))
        if getattr(decision, "verdict", None) != "ALLOW":
            layer = str(getattr(decision, "layer", "gate_error"))
            reason = getattr(decision, "reason", None) or "MCP tool call was not authorized"
            logger.warning(
                "h_gated_mcp_tool denied tool=%s layer=%s reason=%s",
                config.mcp_tool_name,
                layer,
                reason,
            )
            return GatedMcpResponse(status="denied", reason=str(reason), gate_layer=layer)

        output = await raw_function.ainvoke(request, to_type=str)
        return GatedMcpResponse(status="ok", output=output)

    yield FunctionInfo.create(
        single_fn=invoke,
        input_schema=input_schema,
        description=f"Safety-gated MCP tool: {raw_function.description}",
        converters=[string_to_request, _response_to_str],
    )
