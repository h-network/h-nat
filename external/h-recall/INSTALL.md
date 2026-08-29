# INSTALL — `h-recall`

Installation and verification guide for the `h-recall` NAT plugin.

---

## 1. Prerequisites

- **Python**: `>=3.11,<3.14`
- **NVIDIA NeMo Agent Toolkit (NAT)**: `nvidia-nat>=1.6,<2` (ADR-008)
- **Redis Stack**: Redis $\ge$ 7.1 with `RediSearch` and `RedisJSON` modules loaded (ADR-004)
- **fastembed Model Cache**: ~70 MB downloaded to `~/.cache/fastembed` on first vectorization/search invocation.

---

## 2. Installation

```bash
# Install editable package
pip install -e external/h-recall
```

---

## 3. Verification

### 3a. Import Check
```bash
python3 -c "
import nat.plugins.h_recall
import nat.plugins.h_network_semantic_memory
print('Imports clean')
"
```

### 3b. NAT Component Discovery
```bash
nat info components -t function | grep -E "h_semantic_(search|sweep|vectorize)"
```

### 3c. Lazy Builder Build Check
```bash
python3 external/h-recall/_verify/check.py
```

Expected output:
```
--- colocated topology (build-check.yaml) ---
  search: built OK (FunctionImpl)
  sweep: built OK (FunctionImpl)
  vectorize: built OK (FunctionImpl)
--- split topology (round-67) (build-check-split.yaml) ---
  search: built OK (FunctionImpl)
  sweep: built OK (FunctionImpl)
  vectorize: built OK (FunctionImpl)
build check: PASS
```
