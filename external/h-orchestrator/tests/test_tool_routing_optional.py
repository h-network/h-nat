"""Conditional routing proof using NAT's real LangChain agent graph.

Install the ``example`` extra to run this test. The default unit environment
skips it because the production plugin does not otherwise require LangChain.
"""

from collections import deque
from typing import Any

import pytest

langchain_core = pytest.importorskip("langchain_core")
pytest.importorskip("nat.plugins.langchain.agent.tool_calling_agent.agent")

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from nat.plugins.langchain.agent.tool_calling_agent.agent import (
    ToolCallAgentGraph,
    ToolCallAgentGraphState,
)


class RecallInput(BaseModel):
    chat_id: str = Field(description="Conversation to search")
    query: str = Field(description="Older fact to find")
    top_k: int = 3
    mode: str = "hybrid"


class ScriptedToolCallingModel(BaseChatModel):
    responses: deque[AIMessage]

    def __init__(self, responses):
        super().__init__(responses=deque(responses))

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=self.responses.popleft())]
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling-model"


async def run_graph(responses, recall_calls):
    async def recall_search(chat_id: str, query: str, top_k: int, mode: str):
        recall_calls.append(
            {"chat_id": chat_id, "query": query, "top_k": top_k, "mode": mode}
        )
        return [{"content": "The stored codeword is ORBIT-TEST."}]

    tool = StructuredTool.from_function(
        coroutine=recall_search,
        name="recall_search",
        description="Search older conversation facts",
        args_schema=RecallInput,
    )
    agent = ToolCallAgentGraph(
        llm=ScriptedToolCallingModel(responses),
        tools=[tool],
        prompt="Use recall only when current context is insufficient.",
    )
    graph = await agent.build_graph()
    state = await graph.ainvoke(
        ToolCallAgentGraphState(messages=[HumanMessage(content="question")]),
        config={"recursion_limit": 6},
    )
    return ToolCallAgentGraphState(**state)


@pytest.mark.asyncio
async def test_self_contained_question_does_not_call_recall():
    calls = []
    state = await run_graph([AIMessage(content="4")], calls)
    assert state.messages[-1].content == "4"
    assert calls == []


@pytest.mark.asyncio
async def test_missing_old_fact_calls_recall_with_typed_arguments():
    calls = []
    tool_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "recall_search",
                "args": {
                    "chat_id": "recall-demo-chat",
                    "query": "remembered codeword",
                    "top_k": 3,
                    "mode": "hybrid",
                },
                "id": "recall-1",
                "type": "tool_call",
            }
        ],
    )
    state = await run_graph(
        [tool_call, AIMessage(content="The codeword is ORBIT-TEST.")], calls
    )
    assert state.messages[-1].content == "The codeword is ORBIT-TEST."
    assert calls == [
        {
            "chat_id": "recall-demo-chat",
            "query": "remembered codeword",
            "top_k": 3,
            "mode": "hybrid",
        }
    ]
