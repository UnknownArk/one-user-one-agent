# System Architecture: One User, One Agent

## 1. Ownership & Agent Boundary
- **Single Agent per Viewer**: Every user is assigned a dedicated lifecycle agent that strictly owns:
  - The single active goal (`OPEN` status in `viewer_goals`).
  - The durable history of facts, decisions, actions, and lessons learned.
- **Split-Brain Invariant**: No two subsystems can pursue different goals or interventions for the same viewer. Whenever a new goal is initiated, any prior open goal is atomically cancelled in the state store with a recorded reason (`superceded_by_new_goal`).
- **Idempotency**: The trigger's `event_id` serves as the global idempotency key. Duplicate triggers arriving at different delivery times return the cached decision and emit zero duplicate actions.

---

## 2. Planning & Goal Lifecycle
- **Eligibility Grounded in Evidence**: Candidate goals (`G01`–`G12`) are evaluated deterministically against verifiable events in SQLite (`play_started`, `episode_completed`, `purchase_completed`).
- **Explicit Rejection of Unmeasurable Goals**: Goal `G10` (`meaningful_engagement_7d`) contains `"event": null`. It is blacklisted at initialization with an audit trail, ensuring the system never commits to unmeasurable objectives.
- **Dynamic Re-planning on Situation Change**: Goal re-planning occurs event-driven (e.g., when a `purchase_completed` arrives during replay for a user on a trial or purchase goal), immediately closing the conversion goal as succeeded and transitioning to engagement goals (`G07` or `G08`).
- **Mid-Replay Rule Corrections**: When operational events (`goal_rule_corrected`) arrive (such as `G03` requiring `play_started` instead of `app_open`), the correction is applied dynamically only to currently `OPEN` goals of that `goal_id`. Completed history is preserved without retroactive reprocessing.

---

## 3. Memory & Provenance Model
- **Durable State Storage**: State is backed by an ACID-compliant SQLite store (`stage_agent.db`) configured with Write-Ahead Logging (`WAL`) and synchronous `NORMAL` for ultra-low latency.
- **Fact Provenance Tuples**: Every viewer fact is stored as:
  $$\text{Fact} = (\text{key}, \text{value}, \text{source}, \text{observed\_at}, \text{freshness\_s})$$
- **Explicit Conflict Precedence Rules**:
  1. **Dialect**: The current event's `app_dialect` takes strict precedence as the ground truth for UI rendering. Any discrepancy with `profile_snapshot` or campaign `deeplink` is preserved in the decision record's `dialect.conflicts` array for auditing.
  2. **Subscription Status**: Event stream events (`purchase_completed`, `subscription_renewed`, `subscription_lapsed`) take precedence over warehouse snapshot profiles.
  3. **Duplicate Profiles**: For users appearing multiple times in `profiles_snapshot.csv`, records are reconciled against the event stream rather than trusting `updated_at` timestamps alone.
  4. **Missing Profile Users**: Brand-new users without warehouse rows have their state synthesized directly from lifecycle events (`signup_completed`, `app_open`).

---

## 4. Verification & Trust Boundaries
- **Zero-Trust for Model Output**: Natural language or JSON returned by a model is treated strictly as an unvalidated proposal. Model text is **never** treated as evidence or approval.
- **Independent Gatekeeper**: The `PolicyVerifier` executes in isolation after the decision planner and before any action can be committed or published:
  - **Holdout Arm Isolation**: Verifies `replay/arms.csv`. If viewer is in `holdout`, action publication is vetoed, and the decision is recorded as `HOLDOUT_SUPPRESSED`.
  - **Quiet Hours Enforcement**: Validates that all push and WhatsApp messages fall strictly between 08:00 and 22:00 IST.
  - **Frequency Cap Enforcement**: Enforces a hard limit of at most 1 message (push + WhatsApp combined) per viewer per IST calendar day.
  - **Consent Verification**: Verifies `notifications_opt_in` and `whatsapp_opt_in`, ensuring any later `notification_opt_out` event overrides profile snapshot permissions.
  - **Catalogue & Availability Consistency**: Verifies that every title exists, matches the page dialect, and satisfies $available\_from \le published\_at < available\_until$.
  - **Evidence Grounding**: Cross-references cited evidence identifiers (`event:<uuid>`) against the event store to reject fabricated citations.
- **Catalogue Prompt Injection Neutralization**: Catalogue descriptions are treated purely as data. Regex pattern scanners detect and redact embedded system instructions (e.g. titles `T0002` and `T0522`), preventing prompt injection attacks from reaching downstream models.

---

## 5. Latency & Budget Caps
- **Time Budget**: Mandates publication within 60,000 ms. In practice, the system achieves **p50 = 1 ms, p95 = 4 ms, p99 = 5 ms, max = 34 ms**.
- **Call Budgets**: Enforces configurable caps of maximum 8 model calls and 20 tool calls per decision.
- **Graceful Failure Degradation**:
  - **Model Timeouts & Malformed JSON**: On timeout or JSON decode errors, the system catches the fault and falls back immediately to deterministic heuristic ranking.
  - **Service Outages (HTTP 503)**: During active dependency outages (e.g., model provider down from 10:35 to 10:42 UTC), the system bypasses model calls entirely and operates in pure rule-based mode.
  - **Stale Trigger Bursts**: Offline-buffered app opens ($deliver\_at - occurred\_at > 600\text{ s}$) are flagged with `no_action_reason: "stale_trigger"` and abstained, preventing message floods.
