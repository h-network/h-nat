# Chat with automatic hot memory and recall as a tool

This example composes three independently useful layers:

```text
h_chat_cycle (always read/write recent h-memory turns)
    -> recall_agent (NAT tool_calling_agent)
        -> OpenAI-compatible LLM
        -> recall_search (h_semantic_search, called only by LLM choice)
```

The outer cycle—not the client and not the LLM—owns recent conversational
continuity. `recall_search` is merely available to the agent; the system prompt
tells the model to call it only when the current conversation lacks a needed
fact.

## Requirements

- Python 3.11-3.13.
- Redis Stack with RedisJSON and RediSearch at one URL.
- An OpenAI Chat Completions-compatible endpoint whose model supports native
  tool calling (vLLM and other self-hosted implementations are supported).
- The memory, recall, and orchestrator h-nat packages plus NAT's LangChain
  agent plugin. This composition does not require h-openshell.

From the repository root, install the local modules before the example extra
so pip does not try to resolve unpublished sibling packages:

```bash
pip install -e external/h-memory
pip install -e external/h-recall
pip install -e 'external/h-orchestrator[example]'
```

Configure the endpoints. vLLM commonly accepts an arbitrary non-empty API key:

```bash
export H_NAT_REDIS_URL=redis://127.0.0.1:6379
export H_NAT_LLM_BASE_URL=http://127.0.0.1:8000/v1
export H_NAT_LLM_MODEL=my-tool-capable-model
export OPENAI_API_KEY=EMPTY
```

Validate component discovery and references without contacting the endpoints:

```bash
for config in workflow.yaml sweep.yaml vectorize.yaml; do
  nat validate --config_file \
    "examples/h-orchestrator/hot-memory-recall-tool/$config"
done
```

## Run the automated proof

```bash
python examples/h-orchestrator/hot-memory-recall-tool/run_demo.py
```

The driver uses a random codeword and performs five checks:

1. `workflow.yaml` sends the codeword through `h_chat_cycle`, which writes the
   user and assistant turns to the hot tier.
2. After the one-second demo threshold, `sweep.yaml` moves those turns to the
   audit tier and removes them from hot memory; `vectorize.yaml` embeds them.
3. A self-contained arithmetic question must not produce a
   `recall_search` tool-call trace.
4. Asking for the now-migrated codeword must produce a `recall_search` call.
5. The final answer must contain the exact random codeword, proving it did not
   come from the current hot prompt or model prior knowledge.

`recall_agent.verbose` is intentionally enabled because the verifier checks
NAT's `Calling tools: ['recall_search']` trace. Disable verbose logging in a
production deployment after routing has been verified.

## Run turns manually

Each `nat run` process can exit between turns because conversational state is
in Redis:

```bash
nat run --config_file examples/h-orchestrator/hot-memory-recall-tool/workflow.yaml \
  --input 'Remember that my deployment color is amber.'

nat run --config_file examples/h-orchestrator/hot-memory-recall-tool/workflow.yaml \
  --input 'What deployment color did I choose?'
```

These two immediate turns demonstrate automatic hot memory. The maintenance
and long-term recall sequence is intentionally explicit because h-recall's
sweep/vectorize operations are operator-scheduled, not background daemons.

## Important invariants

- `recall-demo`, `assistant`, and `recall-demo-chat` must match across all three
  YAML files and the agent system prompt. `h_semantic_search` requires the LLM
  to supply `chat_id`, so the fixed example ID is stated explicitly.
- Run sweep before count-based hot eviction. A turn removed from the hot ZSET
  is no longer discoverable by the sweep even if its TTL data key still exists.
- The search tool is hybrid and therefore the vectorize pass is required.
- The example does not clear Redis. It uses a random fact so repeated runs are
  distinguishable while preserving prior audit history.
