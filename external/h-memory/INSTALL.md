# INSTALL — `h-memory`

Installation and post-install verification procedure for the `h-memory` NAT plugin.

---

## 1. Prerequisites

- **Python `>=3.11,<3.14`** (Python 3.12 recommended).
- **Redis 7.x** reachable at configured endpoint (default `redis://localhost:6379`). Plain core Redis is sufficient (no Redis Stack modules required).
- **NAT Toolkit**: `nvidia-nat>=1.6,<2` per ADR-008.

---

## 2. Installation

Install NAT and `h-memory` in editable mode:

```bash
# 1. Install NAT dependency
pip install "nvidia-nat>=1.6,<2"

# 2. Install h-memory (editable)
pip install -e external/h-memory
```

---

## 3. Verification

### 3a. Package Metadata
```bash
pip show h-memory
```
Expected: Package metadata displayed with `redis` and `pydantic` listed under dependencies.

### 3b. NAT Plugin Discovery
```bash
nat info components -t function 2>/dev/null | grep -E "h_memory_(write_turn|delete_chat)"
```
Expected: Two rows corresponding to `h_memory_write_turn` and `h_memory_delete_chat` from package `h-memory`.

### 3c. Workflow Execution Probe
Create a temporary probe configuration `/tmp/h-memory-probe.yaml`:

```yaml
general:
  use_uvloop: true

workflow:
  _type: h_memory_write_turn
  redis_url: redis://localhost:6379
  pod: probe-pod
  agent: probe-agent
  ttl_seconds_max: 86400
```

Execute probe:
```bash
nat run --config_file /tmp/h-memory-probe.yaml \
    --input '{"chat_id": "probe-chat", "role": "user", "content": "hello", "ttl_seconds": 300}'
```
Expected:
- Log line: `h_memory_write_turn connected: redis_url=redis://localhost:6379 pod=probe-pod agent=probe-agent ttl_seconds_max=86400 hot_keep_count=None`
- `Workflow Result:` outputs the written Redis key (e.g., `probe-pod:probe-agent:chat:probe-chat:...`).
- Exit code 0.

### 3d. Run Test Suite
```bash
pytest external/h-memory/tests -v
```
Expected: All 15 tests pass.
