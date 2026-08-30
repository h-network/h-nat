# `h-memory` Stress & Edge-Case Benchmark Suite

A standalone, high-performance stress benchmark suite designed to evaluate the breaking points, throughput, latency profiles, and concurrency invariants of the `h-memory` hot-tier Redis plugin.

---

## Directory Structure

```
benchmark/h-memory/
├── README.md                 # Benchmark guide & scenario definitions
├── vars.example.yaml         # Configuration & knob templates for scenarios
├── workflow_write.yaml       # NAT workflow template for write operations
├── workflow_delete.yaml      # NAT workflow template for chat purging
├── run_bench.py              # Asynchronous driver script with metric calculation
└── RESULTS.md                # Generated results template & performance report
```

---

## Stress & Invariant Scenarios

The suite exercises 5 distinct stress scenarios targeting key Redis pipeline paths and race conditions:

1. **Throughput & Latency Scaling (`throughput`)**:
   - Evaluates sustained write throughput (writes/sec) and latency distributions (min, p50, p90, p95, p99, max) across concurrency levels (10, 50, 100, 200 workers).
   - Validates linear scaling and sub-millisecond pipeline latency.

2. **Concurrent Same-Chat Monotonicity (`concurrent_same_chat`)**:
   - Fires high-concurrency writes (e.g. 50 parallel writers, 1,000 total writes) directly at the *same* `chat_id`.
   - Validates the nanosecond monotonic clock guard (`_next_ts_ns`), asserting **zero key collisions**, 100% write retention, and strict timestamp score ordering in the ZSET index.

3. **Concurrent `hot_keep_count` Rank-Pruning (`rank_prune`)**:
   - Stresses the write pipeline's `ZREMRANGEBYRANK` trimming under high write concurrency with an active rank cap (e.g. $K = 25$).
   - Verifies the ZSET index contains exactly $K$ members corresponding strictly to the most recent turns, while orphaned data keys remain resident in Redis with active TTLs.

4. **TTL Boundary & Expiration Races (`ttl_boundary`)**:
   - Writes short-lived turns (1s TTL) and verifies exact key expiration, conservative index score pruning, and reader-side resilience.

5. **`delete_chat` Racing In-Flight Writes (`delete_race`)**:
   - Fires parallel write streams concurrently with an asynchronous `delete_chat` invocation.
   - Verifies zero deadlocks, zero unhandled pipeline exceptions, and clean post-deletion state consistency.

---

## Running the Benchmark

### Prerequisites

Ensure Redis is running (Redis 7.x or compatible). Set the Redis endpoint via environment variable or use the default localhost:

```bash
export H_NAT_REDIS_URL=redis://localhost:6379
```

### Full Benchmark Run

```bash
python3 benchmark/h-memory/run_bench.py
```

### Quick Sanity Run

```bash
python3 benchmark/h-memory/run_bench.py --quick
```

### Targeted Scenario Execution

```bash
# Run specific scenarios
python3 benchmark/h-memory/run_bench.py --scenarios throughput concurrent_same_chat

# Output machine-readable JSON results
python3 benchmark/h-memory/run_bench.py --json
```

---

## Output & Reports

The driver automatically outputs a formatted Markdown report to [`RESULTS.md`](RESULTS.md), detailing:
- Executive summary table with PASS/FAIL statuses and primary metrics.
- Concurrency scaling tables with latency percentiles.
- JSON metrics for deep scenario analysis.
- Explicit logging of any ordering anomalies, collision rates, or data-loss events.
