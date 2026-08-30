# Long-Chat Stress Test: 200-Turn Conversation Template

## Overview

This directory contains the 200-turn conversation template for the combined **h-memory** (bounded hot-tier Redis buffer) and **h-recall** (long-term semantic/hybrid recall) benchmark.

The objective of the benchmark is to verify that in long, multi-turn conversations across concurrent sessions:
1. **Recent context (Hot tier / `h-memory`)** maintains immediate conversation continuity within a bounded window (e.g. 20–30 turns).
2. **Long-term context (Cold/Hybrid tier / `h-recall`)** reliably indexes and retrieves earlier facts once they have aged out of the hot Redis buffer.

---

## File Structure

- `conversation_template.yaml`: The master 200-turn scripted conversation template with metadata, fact definitions, persona presets, and turn-by-turn definitions.
- `workflow.yaml`: NAT composition joining bounded hot memory, semantic maintenance/search, and the LLM-backed chat cycle.
- `vars.example.yaml`: Example endpoint, tenant, hot-buffer, maintenance, and per-persona chat settings for the load driver.
- `README.md`: Documentation on template structure, persona instantiation, turn distribution, and evaluation grading.

## NAT Workflow Wiring

`workflow.yaml` uses `h_chat_cycle` as the entry point. The cycle persists each
user/assistant pair through h-memory and dispatches to a tool-calling agent
that can invoke `h_semantic_search` when an early fact is absent from the hot
prompt. The same configuration registers `h_semantic_sweep` and
`h_semantic_vectorize` as maintenance functions for the load driver to run
before bounded-buffer eviction makes an old turn undiscoverable.

All memory functions share `${H_NAT_STRESS_POD}` and
`${H_NAT_STRESS_AGENT}`. `${H_NAT_CHAT_ID}` selects the isolated persona
session and is also embedded in the agent's recall-tool instruction. The
example vars assign `long-chat-halil` and `long-chat-ibrahim`; a driver must set
the selected value consistently for the full 200-turn session.

The workflow expects these environment variables, populated from a customized
copy of `vars.example.yaml` by the load driver:

- `H_NAT_LLM_MODEL`, `H_NAT_LLM_BASE_URL`, and `OPENAI_API_KEY`
- `H_NAT_REDIS_URL`
- `H_NAT_STRESS_POD`, `H_NAT_STRESS_AGENT`, and `H_NAT_CHAT_ID`
- `H_NAT_STRESS_HOT_KEEP_COUNT`
- `H_NAT_STRESS_MIGRATION_THRESHOLD_SEC`
- `H_NAT_STRESS_VECTORIZE_BATCH_SIZE`

---

## Turn Breakdown & Design

The template is 200 user turns long, split into distinct functional phases:

| Turn Range | Phase | Description |
|---|---|---|
| **1 – 45** | **Early Context & Fact Introductions** | Natural technical discussion, kickoff, and 10 concrete personal facts introduced exactly once. |
| **46 – 110** | **Buffer Flush & Control Turns** | Deep, realistic multi-domain discussions (distributed systems, OS internals, science, cooking, cryptography) designed to completely flush a 20–30 turn bounded hot-tier memory buffer. |
| **111 – 195** | **Long-Term Recall Probes & Control Turns** | Control turns interspersed with 10 recall probes targeting facts introduced in turns 1–45. |
| **196 – 200** | **Wrap-Up & Summary** | Session summaries, recommendations, and closing turns. |

### Fact Introductions & Recall Probes

Each of the 10 facts is introduced once early in the conversation and probed once late in the conversation. Every probe occurs at least 112 turns (and up to 151 turns) after its introduction, well beyond the reach of hot memory:

| Fact Key | Description | Intro Turn | Probe Turn | Distance ($\Delta$) | Expected Answer Template |
|---|---|---|---|---|---|
| `user_name` | User's first name | Turn 2 | Turn 118 | **116 turns** | `{{user_name}}` |
| `job_title` | Professional job title | Turn 5 | Turn 134 | **129 turns** | `{{job_title}}` |
| `company_name` | Organization / company | Turn 9 | Turn 152 | **143 turns** | `{{company_name}}` |
| `pet_name` | Pet's name | Turn 14 | Turn 126 | **112 turns** | `{{pet_name}}` |
| `pet_type` | Pet's breed / species | Turn 18 | Turn 161 | **143 turns** | `{{pet_type}}` |
| `hometown` | City of origin | Turn 23 | Turn 143 | **120 turns** | `{{hometown}}` |
| `favorite_coffee` | Preferred coffee brew / variety | Turn 28 | Turn 170 | **142 turns** | `{{favorite_coffee}}` |
| `lucky_number` | Personal lucky number / RNG seed | Turn 33 | Turn 179 | **146 turns** | `{{lucky_number}}` |
| `project_codename` | Internal project codename | Turn 38 | Turn 188 | **150 turns** | `{{project_codename}}` |
| `vacation_destination` | Travel / vacation destination | Turn 44 | Turn 195 | **151 turns** | `{{vacation_destination}}` |

---

## Persona Presets

The template includes two predefined persona value sets under `metadata.persona_presets`:

### Persona 1: Halil
```yaml
user_name: "Halil"
job_title: "Distributed Systems Engineer"
company_name: "NovaGrid Labs"
pet_name: "Barnaby"
pet_type: "Golden Retriever"
hometown: "Amsterdam Oost"
favorite_coffee: "Chemex Ethiopian Yirgacheffe"
lucky_number: "7429"
project_codename: "Project Borealis"
vacation_destination: "Kyoto"
```

### Persona 2: Ibrahim
```yaml
user_name: "Ibrahim"
job_title: "Cloud Infrastructure Architect"
company_name: "ApexTelemetry"
pet_name: "Zephyr"
pet_type: "Maine Coon cat"
hometown: "Amsterdam Pijp"
favorite_coffee: "Aeropress Guatemalan Antigua"
lucky_number: "8314"
project_codename: "Project Chimera"
vacation_destination: "Reykjavik"
```

---

## Turn Schema Reference

Each item in the `turns` list contains:

- `turn` (`int`): 1-based turn index (1 to 200).
- `type` (`str`): One of:
  - `fact_introduction`: A turn where a personal fact placeholder is introduced into the conversation.
  - `recall_probe`: A turn where the user queries the assistant for a fact introduced earlier.
  - `control`: A natural conversational question/dialogue turn independent of personal facts.
- `user_message` (`str`): The message sent by the user, containing `{{placeholder}}` tokens where facts are substituted.
- `fact_key` (`str`, optional): The key of the fact being introduced or probed.
- `expected_answer` (`str`, optional): The expected template string for automated grading on recall probes.
- `notes` (`str`): Human-readable topic and context description.

---

## Automated Grading & Verification

For automated benchmark verification:
1. Substitute the persona's key-value map into each turn's `user_message` and `expected_answer`.
2. Send turns sequentially to the chat endpoint.
3. For turns with `type: recall_probe`, evaluate whether the assistant's response contains the resolved `expected_answer` string.
