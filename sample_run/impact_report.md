# Impact and Measurement Report

## 1. Pilot Analysis: Selection Bias vs. Honest ITT

### The Pilot Summary Report Trap
The internal pilot summary report (`pilot/pilot_summary_report.md`) claimed a **2.27x uplift (+22.43 percentage points)**:
- **Served by the agent**: 217 users reached goal at **40.09%**
- **All other users**: 3674 users reached goal at **17.66%**

### Why "Served vs. Everyone Else" Is Seriously Flawed
Comparing served users against unserved users is a classic **selection bias**:
1. The agent only served **217 out of 1912 users** (11.3%) in the treatment arm.
2. The remaining 1695 unserved treatment users were lumped into the "All other users" comparison group along with the holdout arm.
3. Users who got served were already more engaged (had active sessions, completed signups, explored catalogue). They would have achieved higher completion rates organically regardless of whether an agent intervened.

### The Correct Comparison: Intention-to-Treat (ITT)
When we evaluate the true randomized arms:
- **Treatment Arm (All 1912 assigned users)**: **18.25%** success
- **Holdout Arm (All 1979 holdout users)**: **19.56%** success
- **True Pilot ITT Difference**: **-1.3 percentage points** (95% CI: [-3.76, +1.16])

**Conclusion**: The pilot produced **no statistically detectable uplift** (95% CI crosses zero). Rolling out based on the naive 40.1% vs 17.7% comparison would have been a major business error.

---

## 2. Replay Performance: Honest Causal Measurement

### Intention-to-Treat (ITT) Results on Replay Day
- **Agent Arm Users**: 751 (Success: **10.92%**)
- **Holdout Arm Users**: 743 (Success: **11.17%**)
- **Replay ITT Difference**: **-0.25 percentage points** (95% CI: [-3.43, 2.93])

**Evaluation Verdict: No detectable effect.**
Because the 95% confidence interval spans across zero ([-3.43, +2.93]), this must be reported as **no detectable effect**, never as an uplift or improvement. A single replay day provides insufficient statistical power to claim that the agent caused a difference.

### Robustness Across Simulator Seeds (s1, s2, s3)
The outcome simulator was rerun across multiple independent seeds on the exact same `actions.jsonl` to assess variance:

| Simulator Seed | Agent Rate (N) | Holdout Rate (N) | ITT Diff (pts) | 95% Confidence Interval | Verdict |
|---|---|---|---|---|---|
| `s1` | 10.92% (751) | 11.17% (743) | -0.25 | [-3.43, +2.93] | No detectable effect |
| `s2` | 12.65% (751) | 10.63% (743) | +2.02 | [-1.23, +5.27] | No detectable effect |
| `s3` | 12.78% (751) | 10.36% (743) | +2.42 | [-0.82, +5.66] | No detectable effect |

*Across all seeds, the 95% confidence intervals consistently encompass zero, confirming no detectable effect under varying simulated behavioral paths.*

### Goal-Mix Balance Table (Arm Comparability)
To verify that the randomized arms were balanced at baseline, the distribution of assigned candidate goals across the two arms is compared below:

| Goal ID | Agent N (% of Arm) | Holdout N (% of Arm) | Mix Diff (pts) |
|---|---|---|---|
| G01 | 13 (1.73%) | 10 (1.35%) | +0.38 |
| G02 | 240 (31.96%) | 215 (28.94%) | +3.02 |
| G03 | 1 (0.13%) | 1 (0.13%) | +0.00 |
| G04 | 1 (0.13%) | 1 (0.13%) | +0.00 |
| G05 | 36 (4.79%) | 45 (6.06%) | -1.27 |
| G06 | 77 (10.25%) | 92 (12.38%) | -2.13 |
| G07 | 342 (45.54%) | 335 (45.09%) | +0.45 |
| G08 | 30 (3.99%) | 37 (4.98%) | -0.99 |
| G09 | 11 (1.46%) | 7 (0.94%) | +0.52 |

*The goal mix is closely matched between arms (e.g. G07 at 45.5% vs 45.1%, G02 at 32.0% vs 28.9%), verifying that randomization produced comparable arms.*

### Per-Goal Outcome Breakdown
| Goal ID | Agent Rate (N) | Holdout Rate (N) | Outcome Diff (pts) |
|---|---|---|---|
| G01 | 15.38% (13) | 20.0% (10) | -4.62 |
| G02 | 7.92% (240) | 7.91% (215) | 0.01 |
| G03 | 0.0% (1) | 0.0% (1) | 0.0 |
| G04 | 0.0% (1) | 0.0% (1) | 0.0 |
| G05 | 5.56% (36) | 6.67% (45) | -1.11 |
| G06 | 14.29% (77) | 11.96% (92) | 2.33 |
| G07 | 4.09% (342) | 4.78% (335) | -0.68 |
| G08 | 96.67% (30) | 86.49% (37) | 10.18 |
| G09 | 45.45% (11) | 28.57% (7) | 16.88 |

### Secondary ITT on Common Outcome (All Triggered Viewers)
In addition to heterogeneous per-goal success, a secondary Intention-to-Treat analysis was conducted across **all 1746 triggered viewers** evaluating a single objective common outcome: **any `play_started` or `purchase_completed` within 14 days** following the replay day.

| Randomized Arm | Triggered Viewers (N) | Common Outcome Success | Engagement Rate (%) | ITT Difference (pts) | 95% Confidence Interval | Verdict |
|---|---|---|---|---|---|---|
| **Agent Arm** | 863 | 645 | **74.74%** | **+0.33** | [-3.75, +4.42] | **No detectable effect** |
| **Holdout Arm** | 883 | 657 | **74.41%** | (baseline) | (baseline) | (reference) |

*The 95% confidence interval for common engagement spans zero ([-3.75, +4.42]), confirming no statistically detectable effect on overarching platform activity.*

### Statistical Power and Minimum Detectable Effect (MDE)
For our sample sizes (863 treatment vs. 883 holdout) and the observed baseline rate of 74.41%, the statistical sensitivity of the experiment is calculated as:
- **Significance Level ($\\alpha$)**: 0.05 (two-tailed, $z = 1.96$)
- **Statistical Power ($1 - \\beta$)**: 80% ($z = 0.8416$)
- **Minimum Detectable Effect (MDE)**: **5.84 percentage points** (equivalent to a **7.8% relative change** from baseline)

*Any true treatment effect smaller than 5.84 percentage points cannot be reliably distinguished from random noise at standard 80% power on a single replay day.*

### Baseline Comparability in HOLDOUT Arm on Common Outcome
Examining unserved vs. served-eligible viewers within the **HOLDOUT arm only** (where the agent never acts) on this common outcome:

| Holdout Viewer Group | Viewers (N) | Common Outcome Success | Organic Activity Rate (%) | Descriptive Interpretation |
|---|---|---|---|---|
| **Acted-On Baseline** (Eligible for intervention) | 706 | 564 | **79.89%** | High-intent viewers with fresh triggers and catalog match |
| **Abstained Baseline** (Agent chose "Do Nothing") | 177 | 93 | **52.54%** | Inactive viewers / stale bursts / dialect mismatch |

*Descriptive finding: Viewers identified as eligible for intervention organically engage at a substantially higher baseline rate (79.89% vs 52.54%) even when no action is ever delivered. This descriptive difference illustrates why comparing served users against unserved users without ITT conflates pre-existing user intent with causal agent impact.*
---

## 3. Real-Time Latency and Call Budget Compliance
- **Decisions Count**: 2769
- **p50 Latency**: 1 ms
- **p95 Latency**: 5 ms
- **p99 Latency**: 9 ms
- **Max Latency**: 61 ms (Budget: 60,000 ms)
- **Compliance**: 100% of decisions published within budget.
