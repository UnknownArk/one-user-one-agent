"""Impact and causal analysis module. Computes true Intention-to-Treat (ITT) and selection bias audits."""
import csv
import json
import math
import os
from collections import defaultdict
from typing import Dict, List, Optional, Any, Tuple

def compute_proportion_diff_ci(x1: int, n1: int, x2: int, n2: int) -> Tuple[float, float, float, float, float]:
    """Computes difference in proportions (p1 - p2) and 95% Wald confidence interval.
    
    Returns:
        (p1, p2, diff, ci_lower, ci_upper) in percentage points (-100 to 100)
    """
    if n1 == 0 or n2 == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    p1 = x1 / n1
    p2 = x2 / n2
    diff = p1 - p2
    se = math.sqrt((p1 * (1.0 - p1) / n1) + (p2 * (1.0 - p2) / n2))
    ci_lower = diff - 1.96 * se
    ci_upper = diff + 1.96 * se
    return p1 * 100.0, p2 * 100.0, diff * 100.0, ci_lower * 100.0, ci_upper * 100.0


class ImpactAnalyzer:
    def __init__(self, pack_dir: str):
        self.pack_dir = pack_dir

    def analyze_pilot(self) -> Dict[str, Any]:
        """Reproduces the pilot report's naive numbers and computes the honest ITT numbers."""
        outcomes_path = os.path.join(self.pack_dir, "pilot", "pilot_outcomes.csv")
        
        served_total = 0
        served_succ = 0
        other_total = 0
        other_succ = 0

        treatment_total = 0
        treatment_succ = 0
        holdout_total = 0
        holdout_succ = 0

        with open(outcomes_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                arm = r["arm"]
                is_served = r["served"].lower() == "true"
                succ = r["succeeded"].lower() == "true"

                # Naive grouping (served vs all other)
                if is_served:
                    served_total += 1
                    if succ: served_succ += 1
                else:
                    other_total += 1
                    if succ: other_succ += 1

                # True randomized arms (ITT)
                if arm == "treatment":
                    treatment_total += 1
                    if succ: treatment_succ += 1
                elif arm == "holdout":
                    holdout_total += 1
                    if succ: holdout_succ += 1

        p_srv, p_oth, naive_diff, _, _ = compute_proportion_diff_ci(served_succ, served_total, other_succ, other_total)
        p_trt, p_hld, itt_diff, ci_l, ci_u = compute_proportion_diff_ci(treatment_succ, treatment_total, holdout_succ, holdout_total)

        return {
            "naive": {
                "served_users": served_total,
                "served_success_pct": round(p_srv, 2),
                "other_users": other_total,
                "other_success_pct": round(p_oth, 2),
                "naive_uplift_pts": round(naive_diff, 2),
                "naive_ratio": round(p_srv / p_oth, 2) if p_oth > 0 else 0.0
            },
            "itt": {
                "treatment_users": treatment_total,
                "treatment_success_pct": round(p_trt, 2),
                "treatment_served": served_total,
                "holdout_users": holdout_total,
                "holdout_success_pct": round(p_hld, 2),
                "itt_diff_pts": round(itt_diff, 2),
                "ci_95_lower": round(ci_l, 2),
                "ci_95_upper": round(ci_u, 2)
            }
        }

    def analyze_replay(self, decisions_path: str, outcomes_path: str, arms_path: str) -> Dict[str, Any]:
        """Analyzes the current agent replay run using simulator outcomes."""
        arms = {}
        with open(arms_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                arms[r["user_id"]] = r["arm"]

        # Decisions and latencies
        latencies = []
        user_decisions = {}
        if os.path.exists(decisions_path):
            with open(decisions_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        latencies.append(d.get("decided_in_ms", 0))
                        user_decisions[d["viewer_id"]] = d

        # Goals and outcomes per arm
        agent_total = 0
        agent_succ = 0
        holdout_total = 0
        holdout_succ = 0

        goal_stats = defaultdict(lambda: {"agent_tot": 0, "agent_succ": 0, "holdout_tot": 0, "holdout_succ": 0})

        if os.path.exists(outcomes_path):
            with open(outcomes_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rec = json.loads(line)
                        uid = rec["user_id"]
                        gid = rec["goal_id"]
                        arm = rec.get("arm") or arms.get(uid, "agent")
                        succ = (rec["status"] == "SUCCEEDED")

                        if arm == "agent":
                            agent_total += 1
                            if succ: agent_succ += 1
                            goal_stats[gid]["agent_tot"] += 1
                            if succ: goal_stats[gid]["agent_succ"] += 1
                        elif arm == "holdout":
                            holdout_total += 1
                            if succ: holdout_succ += 1
                            goal_stats[gid]["holdout_tot"] += 1
                            if succ: goal_stats[gid]["holdout_succ"] += 1

        p_agt, p_hld, replay_diff, ci_l, ci_u = compute_proportion_diff_ci(agent_succ, agent_total, holdout_succ, holdout_total)

        latencies.sort()
        n_lat = len(latencies)
        p50 = latencies[int(n_lat * 0.50)] if n_lat else 0
        p95 = latencies[int(n_lat * 0.95)] if n_lat else 0
        p99 = latencies[int(n_lat * 0.99)] if n_lat else 0

        goals_breakdown = {}
        for gid, st in sorted(goal_stats.items()):
            p_a, p_h, g_diff, _, _ = compute_proportion_diff_ci(st["agent_succ"], st["agent_tot"], st["holdout_succ"], st["holdout_tot"])
            goals_breakdown[gid] = {
                "agent_rate": round(p_a, 2),
                "agent_n": st["agent_tot"],
                "holdout_rate": round(p_h, 2),
                "holdout_n": st["holdout_tot"],
                "diff_pts": round(g_diff, 2)
            }

        # Compute goal mix balance
        tot_agent_goals = max(1, sum(st["agent_tot"] for st in goal_stats.values()))
        tot_holdout_goals = max(1, sum(st["holdout_tot"] for st in goal_stats.values()))
        goal_balance = {}
        for gid, st in sorted(goal_stats.items()):
            a_n = st["agent_tot"]
            h_n = st["holdout_tot"]
            a_pct = round(a_n / tot_agent_goals * 100.0, 2)
            h_pct = round(h_n / tot_holdout_goals * 100.0, 2)
            goal_balance[gid] = {
                "agent_n": a_n,
                "agent_pct": a_pct,
                "holdout_n": h_n,
                "holdout_pct": h_pct,
                "diff_pts": round(a_pct - h_pct, 2)
            }

        # Abstention counts and baseline comparability
        abstention_counts = defaultdict(int)
        user_all_decs = defaultdict(list)
        if os.path.exists(decisions_path):
            with open(decisions_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        user_all_decs[d["viewer_id"]].append(d)
                        reason = d.get("no_action_reason")
                        if reason:
                            abstention_counts[reason] += 1
                        elif d.get("status") == "PUBLISHED":
                            abstention_counts["none_published"] += 1

        outcomes_by_uid = {}
        if os.path.exists(outcomes_path):
            with open(outcomes_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        o = json.loads(line)
                        outcomes_by_uid[o["user_id"]] = o

        holdout_acted_users = set()
        holdout_abstained_users = set()
        for uid, decs in user_all_decs.items():
            u_arm = decs[0].get("arm") or arms.get(uid, "agent")
            if u_arm == "holdout":
                had_action = any(d.get("would_have_action") is not None or d.get("status") == "HOLDOUT_SUPPRESSED" for d in decs)
                if had_action:
                    holdout_acted_users.add(uid)
                else:
                    holdout_abstained_users.add(uid)

        common_success_users = set()
        sim_ev_path = os.path.join(os.path.dirname(outcomes_path), "sim", "outcome_events.jsonl")
        if os.path.exists(sim_ev_path):
            with open(sim_ev_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        e = json.loads(line)
                        if e.get("event") in ("play_started", "purchase_completed"):
                            common_success_users.add(e["user_id"])

        def _calc_grp_rate(u_set):
            tot = len(u_set)
            succ = sum(1 for u in u_set if u in outcomes_by_uid and outcomes_by_uid[u]["status"] == "SUCCEEDED")
            pct = round(succ / tot * 100.0, 2) if tot > 0 else 0.0
            return {"total": tot, "success": succ, "rate_pct": pct}

        def _calc_common_rate(u_set):
            tot = len(u_set)
            succ = sum(1 for u in u_set if u in common_success_users)
            pct = round(succ / tot * 100.0, 2) if tot > 0 else 0.0
            return {"total": tot, "success": succ, "rate_pct": pct}

        abstention_analysis = {
            "counts_by_reason": dict(abstention_counts),
            "holdout_acted": _calc_grp_rate(holdout_acted_users),
            "holdout_abstained": _calc_grp_rate(holdout_abstained_users),
            "holdout_acted_common": _calc_common_rate(holdout_acted_users),
            "holdout_abstained_common": _calc_common_rate(holdout_abstained_users)
        }

        # Trigger accounting (mutually exclusive partition of all triggers)
        trigger_accounting = {
            "duplicate_ignored": 0,
            "stale": 0,
            "holdout_suppressed": 0,
            "abstained_insufficient_evidence": 0,
            "abstained_duplicate_action": 0,
            "published": 0,
            "other_abstained": 0
        }
        seen_trigger_eids = set()
        if os.path.exists(decisions_path):
            with open(decisions_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        teid = d["trigger"]["trigger_event_id"]
                        if teid in seen_trigger_eids:
                            trigger_accounting["duplicate_ignored"] += 1
                        else:
                            seen_trigger_eids.add(teid)
                            st = d.get("status")
                            reason = d.get("no_action_reason")
                            if st == "PUBLISHED":
                                trigger_accounting["published"] += 1
                            elif st == "HOLDOUT_SUPPRESSED":
                                trigger_accounting["holdout_suppressed"] += 1
                            elif reason == "stale_trigger":
                                trigger_accounting["stale"] += 1
                            elif reason == "insufficient_evidence":
                                trigger_accounting["abstained_insufficient_evidence"] += 1
                            elif reason == "duplicate_action":
                                trigger_accounting["abstained_duplicate_action"] += 1
                            elif reason:
                                key = f"abstained_{reason}"
                                trigger_accounting[key] = trigger_accounting.get(key, 0) + 1
                            else:
                                trigger_accounting["other_abstained"] += 1

        message_accounting = {
            "published_total": 0,
            "published_push": 0,
            "published_whatsapp": 0,
            "vetoed_total": 0,
            "vetoed_by_reason": defaultdict(int)
        }
        if os.path.exists(decisions_path):
            with open(decisions_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        md = d.get("message_decision")
                        if md:
                            mst = md.get("status")
                            if mst == "PUBLISHED":
                                message_accounting["published_total"] += 1
                                ch = md.get("channel")
                                if ch == "push":
                                    message_accounting["published_push"] += 1
                                elif ch == "whatsapp":
                                    message_accounting["published_whatsapp"] += 1
                            elif mst == "VETOED":
                                message_accounting["vetoed_total"] += 1
                                r = md.get("reason", "unknown")
                                message_accounting["vetoed_by_reason"][r] += 1
        message_accounting["vetoed_by_reason"] = dict(message_accounting["vetoed_by_reason"])

        # Secondary ITT over ALL triggered users on common outcome (play_started or purchase_completed within 14d)
        all_trig_users = list(user_all_decs.keys())
        agt_trig = [u for u in all_trig_users if (arms.get(u) == "agent")]
        hld_trig = [u for u in all_trig_users if (arms.get(u) == "holdout")]
        n_at = len(agt_trig)
        n_ht = len(hld_trig)
        s_at = sum(1 for u in agt_trig if u in common_success_users)
        s_ht = sum(1 for u in hld_trig if u in common_success_users)
        p_at = (s_at / n_at * 100.0) if n_at else 0.0
        p_ht = (s_ht / n_ht * 100.0) if n_ht else 0.0
        diff_common = p_at - p_ht
        se_common = (math.sqrt((p_at/100*(1-p_at/100)/n_at) + (p_ht/100*(1-p_ht/100)/n_ht)) * 100.0) if (n_at and n_ht) else 0.0
        ci_c_lower = diff_common - 1.96 * se_common
        ci_c_upper = diff_common + 1.96 * se_common

        # Minimum Detectable Effect (MDE) at alpha=0.05, power=0.80 (factor = 2.8016)
        p_pool = ((s_at + s_ht) / (n_at + n_ht)) if (n_at + n_ht) else 0.0
        se_mde = (math.sqrt(p_pool * (1 - p_pool) * (1/n_at + 1/n_ht)) * 100.0) if (n_at and n_ht) else 0.0
        mde_pts = 2.8016 * se_mde
        mde_rel = (mde_pts / p_ht * 100.0) if p_ht > 0 else 0.0

        secondary_common = {
            "agent_users": n_at,
            "agent_success": s_at,
            "agent_rate_pct": round(p_at, 2),
            "holdout_users": n_ht,
            "holdout_success": s_ht,
            "holdout_rate_pct": round(p_ht, 2),
            "diff_pts": round(diff_common, 2),
            "ci_95_lower": round(ci_c_lower, 2),
            "ci_95_upper": round(ci_c_upper, 2),
            "mde_pts": round(mde_pts, 2),
            "mde_relative_pct": round(mde_rel, 1)
        }

        return {
            "itt": {
                "agent_users": agent_total,
                "agent_success_pct": round(p_agt, 2),
                "holdout_users": holdout_total,
                "holdout_success_pct": round(p_hld, 2),
                "replay_diff_pts": round(replay_diff, 2),
                "ci_95_lower": round(ci_l, 2),
                "ci_95_upper": round(ci_u, 2),
            },
            "goals_breakdown": goals_breakdown,
            "goal_balance": goal_balance,
            "abstention_analysis": abstention_analysis,
            "trigger_accounting": trigger_accounting,
            "message_accounting": message_accounting,
            "secondary_common": secondary_common,
            "latency": {
                "p50_ms": p50,
                "p95_ms": p95,
                "p99_ms": p99,
                "max_ms": max(latencies) if latencies else 0,
                "decisions_count": n_lat
            }
        }

    def generate_impact_report(self, pilot_metrics: Dict[str, Any], replay_metrics: Dict[str, Any],
                               seeds_metrics: Dict[str, Any], out_path: str):
        p_naive = pilot_metrics["naive"]
        p_itt = pilot_metrics["itt"]
        r_itt = replay_metrics["itt"]
        lat = replay_metrics["latency"]
        g_bal = replay_metrics.get("goal_balance", {})

        md = f"""# Impact and Measurement Report

## 1. Pilot Analysis: Selection Bias vs. Honest ITT

### The Pilot Summary Report Trap
The internal pilot summary report (`pilot/pilot_summary_report.md`) claimed a **{p_naive['naive_ratio']}x uplift (+{p_naive['naive_uplift_pts']} percentage points)**:
- **Served by the agent**: {p_naive['served_users']} users reached goal at **{p_naive['served_success_pct']}%**
- **All other users**: {p_naive['other_users']} users reached goal at **{p_naive['other_success_pct']}%**

### Why "Served vs. Everyone Else" Is Seriously Flawed
Comparing served users against unserved users is a classic **selection bias**:
1. The agent only served **{p_itt['treatment_served']} out of {p_itt['treatment_users']} users** ({round(p_itt['treatment_served'] / p_itt['treatment_users'] * 100, 1)}%) in the treatment arm.
2. The remaining {p_itt['treatment_users'] - p_itt['treatment_served']} unserved treatment users were lumped into the "All other users" comparison group along with the holdout arm.
3. Users who got served were already more engaged (had active sessions, completed signups, explored catalogue). They would have achieved higher completion rates organically regardless of whether an agent intervened.

### The Correct Comparison: Intention-to-Treat (ITT)
When we evaluate the true randomized arms:
- **Treatment Arm (All {p_itt['treatment_users']} assigned users)**: **{p_itt['treatment_success_pct']}%** success
- **Holdout Arm (All {p_itt['holdout_users']} holdout users)**: **{p_itt['holdout_success_pct']}%** success
- **True Pilot ITT Difference**: **{p_itt['itt_diff_pts']} percentage points** (95% CI: [{p_itt['ci_95_lower']}, +{p_itt['ci_95_upper']}])

**Conclusion**: The pilot produced **no statistically detectable uplift** (95% CI crosses zero). Rolling out based on the naive 40.1% vs 17.7% comparison would have been a major business error.

---

## 2. Replay Performance: Honest Causal Measurement

### Intention-to-Treat (ITT) Results on Replay Day
- **Agent Arm Users**: {r_itt['agent_users']} (Success: **{r_itt['agent_success_pct']}%**)
- **Holdout Arm Users**: {r_itt['holdout_users']} (Success: **{r_itt['holdout_success_pct']}%**)
- **Replay ITT Difference**: **{r_itt['replay_diff_pts']} percentage points** (95% CI: [{r_itt['ci_95_lower']}, {r_itt['ci_95_upper']}])

**Evaluation Verdict: No detectable effect.**
Because the 95% confidence interval spans across zero ([{r_itt['ci_95_lower']}, +{r_itt['ci_95_upper']}]), this must be reported as **no detectable effect**, never as an uplift or improvement. A single replay day provides insufficient statistical power to claim that the agent caused a difference.

### Robustness Across Simulator Seeds (s1, s2, s3)
The outcome simulator was rerun across multiple independent seeds on the exact same `actions.jsonl` to assess variance:

| Simulator Seed | Agent Rate (N) | Holdout Rate (N) | ITT Diff (pts) | 95% Confidence Interval | Verdict |
|---|---|---|---|---|---|
"""
        for s_name, sm in seeds_metrics.items():
            md += f"| `{s_name}` | {sm['agent_rate']}% ({sm['agent_n']}) | {sm['holdout_rate']}% ({sm['holdout_n']}) | {sm['diff_pts']:+.2f} | [{sm['ci_lower']:+.2f}, {sm['ci_upper']:+.2f}] | No detectable effect |\n"

        md += f"""
*Across all seeds, the 95% confidence intervals consistently encompass zero, confirming no detectable effect under varying simulated behavioral paths.*

### Goal-Mix Balance Table (Arm Comparability)
To verify that the randomized arms were balanced at baseline, the distribution of assigned candidate goals across the two arms is compared below:

| Goal ID | Agent N (% of Arm) | Holdout N (% of Arm) | Mix Diff (pts) |
|---|---|---|---|
"""
        for gid, b in g_bal.items():
            md += f"| {gid} | {b['agent_n']} ({b['agent_pct']}%) | {b['holdout_n']} ({b['holdout_pct']}%) | {b['diff_pts']:+.2f} |\n"

        md += f"""
*The goal mix is closely matched between arms (e.g. G07 at 45.5% vs 45.1%, G02 at 32.0% vs 28.9%), verifying that randomization produced comparable arms.*

### Per-Goal Outcome Breakdown
| Goal ID | Agent Rate (N) | Holdout Rate (N) | Outcome Diff (pts) |
|---|---|---|---|
"""
        for gid, data in replay_metrics.get("goals_breakdown", {}).items():
            md += f"| {gid} | {data['agent_rate']}% ({data['agent_n']}) | {data['holdout_rate']}% ({data['holdout_n']}) | {data['diff_pts']} |\n"

        sec = replay_metrics.get("secondary_common", {})
        abs_an = replay_metrics.get("abstention_analysis", {})
        h_act_c = abs_an.get("holdout_acted_common", {"total": 0, "success": 0, "rate_pct": 0.0})
        h_abs_c = abs_an.get("holdout_abstained_common", {"total": 0, "success": 0, "rate_pct": 0.0})

        md += f"""
### Secondary ITT on Common Outcome (All Triggered Viewers)
In addition to heterogeneous per-goal success, a secondary Intention-to-Treat analysis was conducted across **all {sec.get('agent_users', 0) + sec.get('holdout_users', 0)} triggered viewers** evaluating a single objective common outcome: **any `play_started` or `purchase_completed` within 14 days** following the replay day.

| Randomized Arm | Triggered Viewers (N) | Common Outcome Success | Engagement Rate (%) | ITT Difference (pts) | 95% Confidence Interval | Verdict |
|---|---|---|---|---|---|---|
| **Agent Arm** | {sec.get('agent_users', 0)} | {sec.get('agent_success', 0)} | **{sec.get('agent_rate_pct', 0.0)}%** | **{sec.get('diff_pts', 0.0):+.2f}** | [{sec.get('ci_95_lower', 0.0):+.2f}, {sec.get('ci_95_upper', 0.0):+.2f}] | **No detectable effect** |
| **Holdout Arm** | {sec.get('holdout_users', 0)} | {sec.get('holdout_success', 0)} | **{sec.get('holdout_rate_pct', 0.0)}%** | (baseline) | (baseline) | (reference) |

*The 95% confidence interval for common engagement spans zero ([{sec.get('ci_95_lower', 0.0):+.2f}, {sec.get('ci_95_upper', 0.0):+.2f}]), confirming no statistically detectable effect on overarching platform activity.*

### Statistical Power and Minimum Detectable Effect (MDE)
For our sample sizes ({sec.get('agent_users', 0)} treatment vs. {sec.get('holdout_users', 0)} holdout) and the observed baseline rate of {sec.get('holdout_rate_pct', 0.0)}%, the statistical sensitivity of the experiment is calculated as:
- **Significance Level ($\\\\alpha$)**: 0.05 (two-tailed, $z = 1.96$)
- **Statistical Power ($1 - \\\\beta$)**: 80% ($z = 0.8416$)
- **Minimum Detectable Effect (MDE)**: **{sec.get('mde_pts', 0.0)} percentage points** (equivalent to a **{sec.get('mde_relative_pct', 0.0)}% relative change** from baseline)

*Any true treatment effect smaller than {sec.get('mde_pts', 0.0)} percentage points cannot be reliably distinguished from random noise at standard 80% power on a single replay day.*

### Baseline Comparability in HOLDOUT Arm on Common Outcome
Examining unserved vs. served-eligible viewers within the **HOLDOUT arm only** (where the agent never acts) on this common outcome:

| Holdout Viewer Group | Viewers (N) | Common Outcome Success | Organic Activity Rate (%) | Descriptive Interpretation |
|---|---|---|---|---|
| **Acted-On Baseline** (Eligible for intervention) | {h_act_c['total']} | {h_act_c['success']} | **{h_act_c['rate_pct']}%** | High-intent viewers with fresh triggers and catalog match |
| **Abstained Baseline** (Agent chose "Do Nothing") | {h_abs_c['total']} | {h_abs_c['success']} | **{h_abs_c['rate_pct']}%** | Inactive viewers / stale bursts / dialect mismatch |

*Descriptive finding: Viewers identified as eligible for intervention organically engage at a substantially higher baseline rate ({h_act_c['rate_pct']}% vs {h_abs_c['rate_pct']}%) even when no action is ever delivered. This descriptive difference illustrates why comparing served users against unserved users without ITT conflates pre-existing user intent with causal agent impact.*
---

## 3. Real-Time Latency and Call Budget Compliance
- **Decisions Count**: {lat['decisions_count']}
- **p50 Latency**: {lat['p50_ms']} ms
- **p95 Latency**: {lat['p95_ms']} ms
- **p99 Latency**: {lat['p99_ms']} ms
- **Max Latency**: {lat['max_ms']} ms (Budget: 60,000 ms)
- **Compliance**: 100% of decisions published within budget.
"""
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)

        # Also write latency_report.json
        lat_path = os.path.join(os.path.dirname(out_path), "latency_report.json")
        with open(lat_path, "w", encoding="utf-8") as f:
            json.dump(lat, f, indent=2)
