# `h-recall` Performance, Stress & Accuracy Benchmark

Comprehensive benchmarking suite for **`h-recall`** designed to evaluate semantic discrimination accuracy, volume scaling, concurrency resilience, and adversarial query injection resistance against live Redis Stack.

---

## Benchmark Scenarios

1. **Scenario 1: Semantic Discrimination & Near-Neighbor Confusion Matrix**
   - Plants overlapping, semantically adjacent facts across three technical domains (cryptographic algorithms, datacenter disaster recovery tiers, and oncall escalation roles).
   - Measures Top-1 accuracy, Top-3 recall, distractor ranking, and Mean Reciprocal Rank (MRR).

2. **Scenario 2: Volume Scaling & Search Degradation**
   - Populates a large haystack (configurable from 50 to 500+ documents) with realistic engineering logs.
   - Measures sweep throughput (docs/s), vectorization throughput (docs/s), search latency distributions (mean, p50, p95, p99 ms), and needle retrieval accuracy across depth percentiles (10%, 50%, 90%).

3. **Scenario 3: Concurrent Write / Sweep / Vectorize Interleaving**
   - Simulates high-contention production loads with multiple concurrent writer threads pushing turns to hot memory while continuous background sweep and vectorization workers run in parallel.
   - Validates the 11 core invariants: zero turn loss (100% audit retention), zero duplicate records, idempotency deduplication, and complete vectorization convergence.

4. **Scenario 4: Adversarial Query Sanitization & Injection Stress**
   - Submits 25+ malicious, unescaped, and malformed RediSearch query payloads (syntax punctuation floods, field tag hijacking, KNN vector injections, unbalanced parens/quotes, Unicode/emoji floods).
   - Verifies that `escape_redisearch_query` neutralizes injection attacks with zero query syntax errors or unhandled exceptions.

---

## Configuration & Setup

### 1. Prerequisites

Ensure `h-recall`, `h-memory`, and `nvidia-nat` are installed in your Python environment:

```bash
pip install -e external/h-memory
pip install -e external/h-recall
```

### 2. Environment Variables / Configuration File

Copy the example variables file:

```bash
cp benchmark/h-recall/vars.example.yaml benchmark/h-recall/vars.yaml
```

Or set the environment variable:

```bash
export H_NAT_REDIS_URL=redis://172.16.10.102:6379   # Target Redis Stack URL
```

---

## Running the Benchmarks

### Run Full Benchmark Suite

```bash
python benchmark/h-recall/run_bench.py
```

### Run Individual Scenarios

```bash
# Scenario 1 only (Semantic Discrimination)
python benchmark/h-recall/run_bench.py --scenario 1

# Scenario 2 only with custom document volume
python benchmark/h-recall/run_bench.py --scenario 2 --scale 250

# Scenario 3 only with custom worker count
python benchmark/h-recall/run_bench.py --scenario 3 --workers 12

# Scenario 4 only (Adversarial Query Sanitization)
python benchmark/h-recall/run_bench.py --scenario 4
```

---

## Workflow Configurations

- [`workflow.yaml`](workflow.yaml) — Combined NAT workflow declaring all three functions (`sweep`, `vectorize`, `search`).
- [`workflow_sweep.yaml`](workflow_sweep.yaml) — Sweep function workflow.
- [`workflow_vectorize.yaml`](workflow_vectorize.yaml) — Vectorize function workflow.
- [`workflow_search.yaml`](workflow_search.yaml) — Search function workflow.

Validate configs:

```bash
nat validate --config_file benchmark/h-recall/workflow.yaml
```

---

## Results & Recording

Benchmark results are recorded in [`RESULTS.md`](RESULTS.md).
