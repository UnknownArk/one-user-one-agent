# Known Limitations & Production Roadmap

This document outlines deliberate architectural trade-offs, operational realities of the replay run, and scaling pathways for 10 million viewers across 40 regional dialects.

---

## 1. Operational Realities of the Replay Run

### A. Home Pages as the Primary Published Channel
- **Replay Behavior**: Personalized `home_page` layouts were the sole action type published during this replay day run (1,062 published actions). Because every trigger in `triggers.jsonl` was an `app_open` or `login_success` session start, personalizing the viewer's immediate landing surface was prioritized.
- **Other Action Paths in Codebase**:
  - `push`: Title ($\le 40$ chars), body ($\le 120$ chars), deeplink (`ott://title/<id>`), forbidden phrase scanner, and pricing validation.
  - `whatsapp`: Restricted strictly to the 3 approved templates (`wa_new_episode_v2`, `wa_recommend_title_v1`, `wa_trial_ending_v1`) with catalogue and pricing variable substitution.
  - `in_app_message`: Queued for next app open.
- **Unit-Tested Message Paths**: Verification of messaging consent (`notifications_opt_in`, `whatsapp_opt_in`, and overriding `notification_opt_out` events), quiet hours enforcement (22:00–08:00 IST), daily frequency caps (max 1 message/IST day), and holdout vetoes are fully tested in `tests/test_agent_suite.py`.

### B. Goal Outcome Judgment: Failed vs. Inconclusive (Why 0 Inconclusive)
- **Judgment Criteria**:
  - `SUCCEEDED`: An attributable event matching the goal's success condition occurred between goal start and deadline ($start \le received\_at \le deadline$).
  - `FAILED`: The goal deadline passed with no attributable success event observed.
  - `INCONCLUSIVE`: Reserved for cases where the telemetry observation horizon ends while the goal window is still open without a success event.
- **Why 0 Inconclusive Occurred**: The simulation horizon runs for 14 days (336 hours) post-replay. The maximum candidate goal window in `goals.json` is 14 days (336 hours for `G06`, `G09`, `G11`). Consequently, all open goals reached their deadline within the 14-day observation window, leaving 0 open or inconclusive goals.

### C. Latency Measurement & Fault Injection Timing
- `decided_in_ms` is measured using high-resolution monotonic timestamps (`time.monotonic()`) reflecting actual CPU decision time (p50 = 1 ms, p95 = 4 ms).
- For injected model faults (such as the 45-second timeout in `model_faults.jsonl`), the mock provider raises `ModelTimeoutException` immediately upon matching the fault trigger ID rather than blocking execution for 45 real seconds, allowing the replay to complete in seconds while verifying the fallback branch.

### D. 277 Users With No Goal Assignment
- Exactly 277 of the 1,746 triggered users received no goal and no action.
- **Reason**: These viewers presented exclusively with offline-buffered stale triggers ($deliver\_at - occurred\_at > 600\text{ s}$, up to 8 days old) or duplicate events. The agent safely abstained (`stale_trigger`) to avoid spamming historical visits.

### E. Handling of Negative-Age (Clock Skew) Triggers
- Certain mobile devices had clocks set ahead of server time, resulting in $occurred\_at > deliver\_at$ (negative event age).
- **Handling**: The system flags the clock skew in the decision's `assumptions` array (`"Phone clock ahead of server by Xs (clock skew)"`), clamps the effective freshness calculation, and strictly sequences events by the trustworthy server timestamp (`received_at` / `deliver_at`).

---

## 2. Deliberate Architectural Trade-Offs & Scaling Roadmap

### A. Embedded SQLite vs. Distributed State Store
- **Current State**: Uses an embedded SQLite instance (`stage_agent.db`) with Write-Ahead Logging (`WAL`). It handles the 5,000 users and 2,769 triggers in under 20 seconds on a single machine.
- **Scaling to 10M Users**: A single SQLite file suffers write serialization bottlenecks. Production requires a distributed key-value store (e.g. Redis Cluster for hot state / session caches and ScyllaDB/Cassandra for immutable event history), partitioned by `viewer_id`.

### B. Heuristic Candidate Ranking vs. Deep Embedding Models
- **Current State**: Model ranking uses genre-affinity and release-date scoring with deterministic fallbacks when models time out.
- **Scaling to 10M Users / 40 Dialects**: Deploy an asynchronous Two-Tower neural retrieval pipeline (user history embedding $\times$ dialect title embedding) running as an internal gRPC service with caching, bounded to a strict 15ms timeout budget.

### C. Post-Hoc Goal Attribution vs. Continuous Stream Processing
- **Current State**: Goal outcomes are judged after the simulator completes by scanning attributable events inside the goal window.
- **Scaling to 10M Users**: Implement a continuous stream processing topology (e.g., Apache Flink or Kafka Streams) with dynamic timer windows per viewer that emit `GoalSucceeded` or `GoalExpired` events in real time as user telemetry streams in.
