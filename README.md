# One User, One Agent - STAGE AI Engineering System

An agentic lifecycle system where every viewer has a dedicated agent that chooses a measurable goal, decides proportionate interventions, enforces strict policy rules independently, and measures honest Intention-to-Treat (ITT) causal uplift.

---

## 1. Quickstart (One Command Run)

### Prerequisites
- Python 3.9+ (Standard Library)
- Dependencies: `pydantic`, `pytest`

```bash
# 1. Install minimal dependencies
pip install pydantic pytest

# 2. Run the test suite (17 automated tests covering traps, safety, and fallbacks)
python -m pytest -v tests/test_agent_suite.py

# 3. Run the complete replay pipeline with one command
python -m src.replay --pack one_user_one_agent_pack --out sample_run
```

---

## 2. What This Command Executes
1. **Deduplication & Provenance Ingestion**: Deduplicates events by `event_id` and resolves profile/dialect precedence into a durable SQLite state store.
2. **Replay Timeline Execution**: Streams 2,769 triggers and live events in `deliver_at` order, simulating mid-day rule corrections, dependency 503 outages, and title withdrawals.
3. **Independent Verification Firewall**: Every proposed action passes through an isolated Policy Verifier checking holdout suppression, quiet hours (22:00–08:00 IST), consent, frequency caps, and ground-truth evidence.
4. **Simulator Run**: Executes STAGE's `simulator/simulate_outcomes.py` generating 14-day post-replay outcomes across multiple seeds.
5. **Outcome Judgment**: Judges active goals based only on attributable events within the active window.
6. **Honest Causal Measurement**: Computes Intention-to-Treat (ITT) uplift against the randomized holdout arm and exposes the selection bias in the internal pilot report.

---

## 3. Directory Layout

```
.
├── README.md                 # Setup and run instructions
├── ARCHITECTURE.md           # Deep-dive on system design and trust boundaries
├── AI_COLLABORATION.md       # Audit log of AI suggestions, reviews, and rejections
├── KNOWN_LIMITATIONS.md      # Deliberate engineering boundaries and scaling roadmap
├── requirements.txt          # Minimal dependencies (pydantic, pytest)
├── src/                      # Modular Python codebase (<2,900 lines)
│   ├── config.py             # Schemas, pricing invariants, and IST/UTC helpers
│   ├── state_store.py        # SQLite state store with provenance and idempotency
│   ├── data_loader.py        # Deduplication, profile precedence, injection sanitizer
│   ├── goal_planner.py       # Goal eligibility, unmeasurable goal rejection (G10)
│   ├── model_provider.py     # Mock replay provider reproducing model_faults.jsonl
│   ├── verifier.py           # Independent policy verifier with unilateral veto power
│   ├── decide.py             # 7-step decision loop, budgets, and abstention
│   ├── replay.py             # Main CLI replay harness
│   ├── outcome_judge.py      # Attribution engine judging open goals
│   └── impact.py             # ITT calculation, confidence intervals, and reporting
├── tests/
│   └── test_agent_suite.py   # 17 automated test scenarios (all passing)
├── scripts/
│   └── generate_walkthrough.py # Single-viewer lifecycle trace generator
└── sample_run/
    ├── decisions.jsonl       # Full machine-readable decisions matching schema
    ├── actions.jsonl         # Published actions consumed by simulator
    ├── outcomes.jsonl        # Judged goals with attribution evidence
    ├── latency_report.json   # p50, p95, p99 decision latencies
    ├── impact_report.md      # Pilot selection bias audit & replay ITT analysis
    ├── final_report.md       # Summary of discovered traps, abstentions & results
    ├── WALKTHROUGH.md        # Complete single-viewer trace (U-00045)
    ├── stage_agent.db        # SQLite durable state database
    ├── viewer_state/         # Durable per-viewer state snapshots
    └── sim/                  # Simulator outcome events and policy audit
```
