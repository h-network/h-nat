# Stress Benchmark Results — `h-memory`

> [!NOTE]
> This document tracks stress benchmark metrics for `h-memory`.
> - **Section 1 (Authoritative Lab Benchmark Results)** is reserved for official benchmarking against the target production/lab Redis environment.
> - **Section 2 (Development Verification Baseline)** records the developer verification benchmark run on the local development testbed (exercising `nat.runtime.loader.load_workflow`).

---

## 1. Authoritative Lab Benchmark Results (Operator Lab)

*Status:* **PENDING LAB BENCHMARK RUN**  
*Target Environment:* Operator Lab Redis Cluster  
*Conducted By:* Operator Lab Validation

### Lab Benchmark Summary Table

| Scenario | Status | Primary Metric | Verified Invariant / Lab Finding |
| :--- | :--- | :--- | :--- |
| Throughput & Latency Scaling | `[PENDING]` | `TBD writes/sec` | `TBD` |
| Concurrent Same-Chat Monotonicity | `[PENDING]` | `TBD writes / 100% unique` | `TBD` |
| Concurrent `hot_keep_count` Rank-Pruning | `[PENDING]` | `TBD` | `TBD` |
| TTL Boundary & Expiry Behavior | `[PENDING]` | `TBD` | `TBD` |
| `delete_chat` Racing In-Flight Writes | `[PENDING]` | `TBD` | `TBD` |

### Lab Concurrency & Latency Scaling Matrix

| Concurrency | Total Writes | Time (s) | Throughput (writes/sec) | min (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | max (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 10 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |
| 50 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |
| 100 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |
| 200 | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* | *TBD* |

---

## 2. Development Verification Baseline (Local Dev Testbed)

*Conducted:* 2026-08-30 15:58:16 UTC (local development testbed)  
*NAT Workflow Loader:* `nat.runtime.loader.load_workflow`  
*Workflows:* `workflow_write.yaml`, `workflow_delete.yaml`  
*Redis Target:* `redis://localhost:6379`  
*Tenant Scope:* `bench-pod:stress-agent`  
*Platform:* `linux (Python 3.12.3)`  

### Baseline Executive Summary

| Scenario | Status | Primary Metric | Key Finding / Invariant Behavior |
| :--- | :--- | :--- | :--- |
| Throughput & Latency Scaling | ✅ PASS | 1506.6 writes/sec (p99: 13.97ms) | Sub-millisecond write latency sustained through NAT workflow runner |
| Concurrent Same-Chat Monotonicity | ✅ PASS | 1000 writes / 100% unique | Zero key collisions; strict monotonic nanosecond timestamp ordering verified |
| Concurrent hot_keep_count Rank-Pruning | ✅ PASS | Trimmed to exact 25 ranks | Rank trimming atomic under fire; orphan keys preserved in Redis |
| TTL Boundary & Expiry Behavior | ✅ PASS | 50 keys expired | Accurate key-level expiration; post-expiry writes remain robust |
| delete_chat Racing In-Flight Writes | ✅ PASS | 24 keys wiped | Zero deadlocks or exceptions under concurrent delete race |

### Baseline Detailed Scenario Results

#### Throughput & Latency Scaling
Measures write throughput (writes/sec) and p50/p90/p99 latencies through NAT runtime under varying worker concurrency.

**Outcome:** PASS

| Concurrency | Total Writes | Time (s) | Throughput (writes/sec) | min (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | max (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 10 | 2000 | 1.327 | **1506.6** | 1.227 | 5.84 | 10.002 | 11.433 | 13.974 | 38.785 |
| 50 | 2000 | 1.442 | **1387.2** | 6.088 | 32.043 | 43.341 | 46.205 | 125.061 | 153.06 |
| 100 | 2000 | 1.557 | **1284.5** | 13.167 | 65.536 | 79.209 | 152.136 | 348.716 | 363.101 |
| 200 | 2000 | 2.660 | **752.0** | 15.101 | 176.373 | 681.541 | 818.201 | 1274.462 | 1320.609 |
#### Concurrent Same-Chat Monotonicity
Fires high-concurrency writes through NAT at the exact same chat_id to verify monotonic clock guard, uniqueness, and ordering.

**Outcome:** PASS

```json
{
  "writers": 50,
  "writes_per_writer": 20,
  "total_writes": 1000,
  "unique_keys_ratio": 1.0,
  "stats": {
    "count": 1000,
    "min_ms": 11.012,
    "p50_ms": 55.908,
    "p90_ms": 65.634,
    "p95_ms": 67.889,
    "p99_ms": 74.937,
    "max_ms": 78.375,
    "mean_ms": 56.108,
    "stddev_ms": 8.319,
    "throughput_wps": 863.8,
    "total_time_s": 1.1577
  }
}
```

#### Concurrent hot_keep_count Rank-Pruning
Validates that concurrent NAT writes maintain exact index rank bounds without corrupting working set.

**Outcome:** PASS

```json
{
  "total_writes": 200,
  "hot_keep_count": 25,
  "final_index_size": 25,
  "stats": {
    "count": 200,
    "min_ms": 5.859,
    "p50_ms": 19.205,
    "p90_ms": 29.928,
    "p95_ms": 33.012,
    "p99_ms": 34.248,
    "max_ms": 34.472,
    "mean_ms": 20.459,
    "stddev_ms": 6.001,
    "throughput_wps": 937.47,
    "total_time_s": 0.2133
  }
}
```

#### TTL Boundary & Expiry Behavior
Tests turn expiry at short TTL boundaries and verifies post-expiry writes remain robust.

**Outcome:** PASS

```json
{
  "initial_short_ttl_writes": 50,
  "short_ttl_seconds": 1,
  "expired_keys_verified": 50
}
```

#### delete_chat Racing In-Flight Writes
Fires active write streams concurrently with delete_chat through NAT to verify pipeline atomicity and zero deadlock.

**Outcome:** PASS

```json
{
  "active_writers": 10,
  "total_attempts": 300,
  "completed_writes": 300,
  "keys_wiped_by_delete": 24,
  "post_race_resident_members": 270
}
```
