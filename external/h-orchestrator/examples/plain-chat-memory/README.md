# Plain chat with automatic hot memory

This example demonstrates ordinary multi-turn conversation with no tools, MCP
server, sandbox, or long-term recall layer:

```text
h_chat_cycle -> chat_completion -> OpenAI-compatible chat LLM
       |
       +-> h-memory bounded Redis turns
```

Every `nat run` invocation is a separate process. Continuity comes from
`h_chat_cycle`, which reads the same Redis chat index before dispatch and writes
the user/assistant pair after a successful response.

## Configure and run

Install the local h-memory and h-orchestrator packages, then set Redis and an
OpenAI-compatible endpoint:

```bash
pip install -e external/h-memory
pip install -e external/h-orchestrator

export H_NAT_REDIS_URL=redis://127.0.0.1:6379
export H_NAT_LLM_BASE_URL=http://127.0.0.1:8000/v1
export H_NAT_LLM_MODEL=my-chat-model
export OPENAI_API_KEY=EMPTY

nat validate --config_file external/h-orchestrator/examples/plain-chat-memory/workflow.yaml
python external/h-orchestrator/examples/plain-chat-memory/run_demo.py
```

The driver chooses a unique `chat_id`, runs ten independent `nat run`
processes, and verifies the Redis sorted-set count before and after every turn.
It plants facts about a name, job, preferred vendor, and pet; asks one unrelated
arithmetic control question; recalls each fact; and requests a final summary.
The random suffix on the name proves the answer came from this run's history.

## Example transcript

The exact wording varies by model; a successful run has this shape:

| Turn | Prior Redis records | User | Expected assistant content |
| ---: | ---: | --- | --- |
| 1 | 0 | My name is Mira-`<nonce>`. Remember it. | acknowledgement |
| 2 | 2 | I work as a network reliability engineer. | acknowledgement |
| 3 | 4 | My preferred network vendor is Juniper. | acknowledgement |
| 4 | 6 | My pet is an axolotl named Pixel. | acknowledgement |
| 5 | 8 | What is 17 plus 25? | `42` |
| 6 | 10 | What name did I tell you? | Mira-`<nonce>` |
| 7 | 12 | What is my job? | network reliability engineer |
| 8 | 14 | Which network vendor did I say I prefer? | Juniper |
| 9 | 16 | What kind of pet do I have, and what is its name? | axolotl, Pixel |
| 10 | 18 | Summarize the four personal facts. | all four planted facts |

After turn 10 the index contains 20 records: ten user turns and ten assistant
turns. The example deliberately leaves them to expire under the configured
one-day TTL so telemetry can be inspected after the run.
