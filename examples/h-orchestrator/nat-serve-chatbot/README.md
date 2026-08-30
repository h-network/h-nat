# NAT-servable chatbot with hot and long-term memory

This example is a standalone chatbot backend served by NAT's FastAPI front
end. `h_chat_cycle` reads and writes a bounded h-memory conversation window,
while a tool-calling agent can use h-recall for facts that have moved into the
semantic long-term tier.

Phase 1 intentionally has no messaging-platform bridge and no safety gate.
Those concerns can wrap this proven backend in later phases without changing
its memory addressing.

## Architecture

```text
REST or WebSocket client
        |
        v
    h_chat_cycle  <---->  h-memory hot Redis turns
        |
        v
 tool-calling agent ----> h_semantic_search
                              |
                              v
                       h-recall audit tier

maintenance endpoints: h_semantic_sweep -> h_semantic_vectorize
```

A router agent is unnecessary in this version because there is only one user
interaction: chat. One NAT configuration exposes the chat workflow plus two
maintenance functions as separate REST endpoints.

## Requirements and configuration

- Install `h-memory`, `h-recall`, `h-orchestrator`, and NAT's LangChain plugin.
- Run Redis Stack with RedisJSON and RediSearch.
- Provide an OpenAI Chat Completions-compatible model with native tool calling.

Set the variables shown in `vars.example.yaml`. The pod, agent, and chat ID
must remain identical across the chat cycle, search tool, and maintenance
functions. This phase-one configuration intentionally serves one explicit
Redis conversation so REST and WebSocket requests share memory. A later
multi-user bridge must select an isolated workflow configuration per external
chat; NAT's generic REST and WebSocket envelope identifiers are not
automatically mapped to h-memory addressing.

Validate and start the backend from the repository root:

```bash
nat validate --config_file examples/h-orchestrator/nat-serve-chatbot/workflow.yaml
nat serve --config_file examples/h-orchestrator/nat-serve-chatbot/workflow.yaml
# Equivalent:
nat start fastapi --config_file examples/h-orchestrator/nat-serve-chatbot/workflow.yaml
```

The configured server listens on `127.0.0.1:8080` and exposes:

- `POST /v1/workflow` for typed `h_chat_cycle` requests.
- `WS /websocket` for NAT WebSocket messages.
- `POST /maintenance/sweep` and `POST /maintenance/vectorize` for long-term
  memory maintenance. Run sweep before bounded hot eviction, then vectorize.

## REST proof

The typed endpoint accepts `message`. Although the underlying schema also
supports addressing overrides, this example uses its configured identity so
the recall tool and hot-memory cycle cannot diverge:

```bash
curl -s http://127.0.0.1:8080/v1/workflow \
  -H 'content-type: application/json' \
  -d '{"message":"My launch code is cobalt-47. Remember it."}'

curl -s http://127.0.0.1:8080/v1/workflow \
  -H 'content-type: application/json' \
  -d '{"message":"What launch code did I give you?"}'
```

## WebSocket request shape

Connect to `ws://127.0.0.1:8080/websocket` and send a NAT user message:

```json
{
  "type": "user_message",
  "schema_type": "generate",
  "id": "ws-turn-1",
  "conversation_id": "chatbot-demo",
  "content": {
    "messages": [{
      "role": "user",
      "content": [{"type": "text", "text": "What launch code did I give you?"}]
    }]
  }
}
```

Read messages until a `system_response_message` has `status: complete`.

## Verified live transcript

The backend was verified against a live OpenAI-compatible vLLM model and Redis
Stack. Port `8080` was occupied on the verification host, so the supported CLI
override was used; setting the migration threshold to one second made the
hot-to-long-term proof quick:

```bash
export H_NAT_CHATBOT_MIGRATION_THRESHOLD_SEC=1
nat serve \
  --config_file examples/h-orchestrator/nat-serve-chatbot/workflow.yaml \
  --port 18080
```

Two independent HTTP requests proved hot-memory persistence:

```console
$ curl -sS http://127.0.0.1:18080/v1/workflow \
    -H 'content-type: application/json' \
    -d '{"message":"My launch code is cobalt-47. Remember it."}'
{"result":"Got it. I'll remember that your launch code is cobalt-47.","chat_id":"chatbot-serve-demo","prior_turn_count":0,"duration_ms":930,"turn_id":"chatbot:assistant:chat:chatbot-serve-demo:1788117305257663759"}

$ curl -sS http://127.0.0.1:18080/v1/workflow \
    -H 'content-type: application/json' \
    -d '{"message":"What launch code did I give you?"}'
{"result":"Your launch code is cobalt-47.","chat_id":"chatbot-serve-demo","prior_turn_count":2,"duration_ms":405,"turn_id":"chatbot:assistant:chat:chatbot-serve-demo:1788117305676837468"}
```

The maintenance endpoints then removed the conversation from the hot prompt
and made it semantically searchable:

```console
$ curl -sS http://127.0.0.1:18080/maintenance/sweep \
    -H 'content-type: application/json' \
    -d '{"chat_ids":["chatbot-serve-demo"]}'
{"value":{"migrated":6,"skipped_existing":0,"skipped_fresh":0,"scanned":6}}

$ curl -sS http://127.0.0.1:18080/maintenance/vectorize \
    -H 'content-type: application/json' -d '{}'
{"value":{"vectorized":6,"scanned":6,"batches":1}}

$ curl -sS http://127.0.0.1:18080/v1/workflow \
    -H 'content-type: application/json' \
    -d '{"message":"What launch code did I give you?"}'
{"result":"Your launch code was cobalt-47.","chat_id":"chatbot-serve-demo","prior_turn_count":0,"duration_ms":3292,"turn_id":"chatbot:assistant:chat:chatbot-serve-demo:1788117372368719537"}
```

The zero `prior_turn_count` in the final response proves the answer came after
hot memory was emptied. The server trace also showed a `recall_search` tool
call before the answer.

The WebSocket request above returned the remembered code as an in-progress
`system_response_message`, followed by the completion frame:

```json
{"type":"system_response_message","parent_id":"ws-turn-1","conversation_id":"chatbot-demo","content":{"text":"{\"result\":\"Your launch code is cobalt-47.\",\"chat_id\":\"chatbot-serve-demo\",\"prior_turn_count\":4}"},"status":"in_progress"}
{"type":"system_response_message","parent_id":"ws-turn-1","conversation_id":"chatbot-demo","content":{"text":null},"status":"complete"}
```
