"""Goal planner and eligibility engine with explicit interpretation of underspecified rules and trap protection."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

from src.config import parse_ts, iso_ts, get_ist_day

logger = logging.getLogger("stage.goal_planner")

# Documented interpretations for underspecified goals
UNDERSPECIFIED_GOAL_CONFIG = {
    "G07_min_days_inactive": 3,   # G07: >= 3 days since last play of that unfinished series
    "G08_release_window_days": 30, # G08: released in the 30 days before goal start
    "G09_max_active_days": 1,     # G09: plays on at most 1 day in the last 14 days
    "G12_absence_days": 14,       # G12: no app_open in the 14 days prior to current trigger
}

class GoalEvaluationResult:
    def __init__(self, goal_id: str, eligible: bool, reason: str, evidence: List[str],
                 assumptions: List[str], window_hours: int, success_signal: Optional[str],
                 condition: Any, rule_version: str = "2026-09-30"):
        self.goal_id = goal_id
        self.eligible = eligible
        self.reason = reason
        self.evidence = evidence
        self.assumptions = assumptions
        self.window_hours = window_hours
        self.success_signal = success_signal
        self.condition = condition
        self.rule_version = rule_version


class GoalPlanner:
    def __init__(self, goals_config: Dict[str, Any], catalogue, episodes):
        self.goals_config = goals_config
        self.catalogue = catalogue
        self.episodes = episodes
        # Dynamic rule overrides (e.g. from control_events: goal_rule_corrected)
        self.rule_overrides: Dict[str, Dict[str, Any]] = {}
        self.rule_versions: Dict[str, str] = {g["goal_id"]: "2026-09-30" for g in goals_config.get("goals", [])}

    def apply_rule_correction(self, goal_id: str, new_success: Dict[str, Any], at: datetime, note: str = ""):
        """Applies mid-replay rule correction (e.g. G03 success changed from app_open to play_started)."""
        new_version = f"corrected_{iso_ts(at)}"
        self.rule_overrides[goal_id] = new_success
        self.rule_versions[goal_id] = new_version
        logger.info("Applied rule correction to %s at %s. New version: %s. Note: %s",
                    goal_id, iso_ts(at), new_version, note)

    def evaluate_all_goals(self, user_id: str, current_time: datetime, viewer_facts: Dict[str, Any],
                           user_events: List[Dict[str, Any]]) -> List[GoalEvaluationResult]:
        """Evaluates eligibility for all candidate goals against verified event evidence."""
        results = []
        for g_def in self.goals_config.get("goals", []):
            gid = g_def["goal_id"]
            res = self.evaluate_single_goal(gid, user_id, current_time, viewer_facts, user_events)
            results.append(res)
        return results

    def evaluate_single_goal(self, goal_id: str, user_id: str, current_time: datetime,
                             viewer_facts: Dict[str, Any], user_events: List[Dict[str, Any]]) -> GoalEvaluationResult:
        g_def = next((g for g in self.goals_config.get("goals", []) if g["goal_id"] == goal_id), None)
        if not g_def:
            return GoalEvaluationResult(goal_id, False, "unknown_goal_id", [], [], 0, None, None)

        rule_ver = self.rule_versions.get(goal_id, "2026-09-30")
        success_def = self.rule_overrides.get(goal_id, g_def.get("success", {}))
        window_hours = success_def.get("window_hours", 24)
        success_signal = success_def.get("event")
        condition = success_def.get("condition")

        # Trap check: Explicitly reject G10 (null success event)
        if goal_id == "G10" or success_signal is None:
            return GoalEvaluationResult(
                goal_id=goal_id,
                eligible=False,
                reason="rejected_unmeasurable_goal_null_success_event",
                evidence=[f"goal_config:{goal_id}"],
                assumptions=["Goals without a machine-measurable event in the data stream must never be chosen."],
                window_hours=window_hours,
                success_signal=None,
                condition=condition,
                rule_version=rule_ver
            )

        status_fact = viewer_facts.get("subscription_status")
        status = status_fact.value if status_fact else "never"
        status_obs = status_fact.observed_at if status_fact else iso_ts(current_time)

        signup_fact = viewer_facts.get("signup_at")
        signup_dt = parse_ts(signup_fact.value) if signup_fact else None
        signup_obs = signup_fact.observed_at if signup_fact else iso_ts(current_time)

        # Pre-filter events up to current_time
        events_so_far = [e for e in user_events if parse_ts(e.get("received_at")) <= current_time]
        plays = [e for e in events_so_far if e.get("event") == "play_started"]
        completions = [e for e in events_so_far if e.get("event") == "episode_completed"]

        # G01: first_play_24h
        if goal_id == "G01":
            if len(plays) > 0:
                return GoalEvaluationResult(goal_id, False, "has_prior_play_started", [{"ref": f"event:{plays[0]['event_id']}", "freshness_s": 0.0}], [], window_hours, success_signal, condition, rule_ver)
            if not signup_dt or (current_time - signup_dt).total_seconds() > 7 * 86400:
                return GoalEvaluationResult(goal_id, False, "signup_older_than_7d", [], [], window_hours, success_signal, condition, rule_ver)
            f_s = round((current_time - signup_dt).total_seconds(), 1) if signup_dt else 0.0
            evidence = [{"ref": f"fact:signup_at:{signup_obs}", "freshness_s": f_s}]
            signup_events = [e for e in events_so_far if e.get("event") == "signup_completed"]
            if signup_events:
                sgn = signup_events[0]
                sgn_dt = parse_ts(sgn.get("received_at"))
                s_fresh = round((current_time - sgn_dt).total_seconds(), 1) if sgn_dt else 0.0
                evidence.append({"ref": f"event:{sgn['event_id']}", "freshness_s": s_fresh})
            return GoalEvaluationResult(
                goal_id, True, "new_user_no_plays",
                evidence,
                ["Assumes viewer will discover catalog on home page."],
                window_hours, success_signal, condition, rule_ver
            )

        # G02: first_full_episode_3d
        if goal_id == "G02":
            if len(completions) > 0:
                return GoalEvaluationResult(goal_id, False, "has_prior_episode_completed", [{"ref": f"event:{completions[0]['event_id']}", "freshness_s": 0.0}], [], window_hours, success_signal, condition, rule_ver)
            evidence = []
            if plays:
                last_p = plays[-1]
                p_dt = parse_ts(last_p.get("received_at"))
                p_fresh = round((current_time - p_dt).total_seconds(), 1) if p_dt else 0.0
                evidence.append({"ref": f"event:{last_p['event_id']}", "freshness_s": p_fresh})
                tid = last_p.get("properties", {}).get("title_id")
                if tid:
                    evidence.append({"ref": f"catalogue:{tid}", "freshness_s": 0.0})
            else:
                evidence.append({"ref": f"fact:status:{status}", "freshness_s": 0.0})
            return GoalEvaluationResult(
                goal_id, True, "never_completed_episode",
                evidence,
                ["Assumes viewer is receptive to completing first episode."],
                window_hours, success_signal, condition, rule_ver
            )

        # G03: second_day_return_2d
        if goal_id == "G03":
            if not signup_dt or (current_time - signup_dt).total_seconds() > 2 * 86400:
                return GoalEvaluationResult(goal_id, False, "signup_older_than_2d", [], [], window_hours, success_signal, condition, rule_ver)
            f_s = round((current_time - signup_dt).total_seconds(), 1) if signup_dt else 0.0
            evidence = [{"ref": f"fact:signup_at:{signup_obs}", "freshness_s": f_s}]
            signup_events = [e for e in events_so_far if e.get("event") == "signup_completed"]
            if signup_events:
                sgn = signup_events[0]
                sgn_dt = parse_ts(sgn.get("received_at"))
                s_fresh = round((current_time - sgn_dt).total_seconds(), 1) if sgn_dt else 0.0
                evidence.append({"ref": f"event:{sgn['event_id']}", "freshness_s": s_fresh})
            return GoalEvaluationResult(
                goal_id, True, "signed_up_within_2d",
                evidence,
                ["Assumes return habit forms within first 48 hours of signup."],
                window_hours, success_signal, condition, rule_ver
            )

        # G04: trial_to_paid_7d
        if goal_id == "G04":
            if status != "trial_active":
                return GoalEvaluationResult(goal_id, False, f"status_is_{status}_not_trial_active", [], [], window_hours, success_signal, condition, rule_ver)
            purchases = [e for e in events_so_far if e.get("event") == "purchase_completed"]
            evidence = []
            if purchases:
                last_p = purchases[-1]
                p_dt = parse_ts(last_p.get("received_at"))
                p_fresh = round((current_time - p_dt).total_seconds(), 1) if p_dt else 0.0
                evidence.append({"ref": f"event:{last_p['event_id']}", "freshness_s": p_fresh})
            else:
                f_s = round((current_time - parse_ts(status_obs)).total_seconds(), 1) if parse_ts(status_obs) else 0.0
                evidence.append({"ref": f"fact:status:{status_obs}", "freshness_s": f_s})
            evidence.append({"ref": "pricing:P_TRIAL:₹1", "freshness_s": 0.0})
            return GoalEvaluationResult(
                goal_id, True, "active_trial_user",
                evidence,
                ["Assumes user has engaged with trial content and requires conversion prompt."],
                window_hours, success_signal, condition, rule_ver
            )

        # G05: first_paid_7d
        if goal_id == "G05":
            if status != "never":
                return GoalEvaluationResult(goal_id, False, f"status_is_{status}_not_never", [], [], window_hours, success_signal, condition, rule_ver)
            f_s = round((current_time - parse_ts(status_obs)).total_seconds(), 1) if parse_ts(status_obs) else 0.0
            evidence = [{"ref": f"fact:subscription_status:never:{status_fact.source if status_fact else 'snapshot'}", "freshness_s": f_s}]
            # Cite events confirming purchase/lapse stream
            purchases_or_lapses = [e for e in events_so_far if e.get("event") in ("purchase_completed", "subscription_lapsed")]
            if purchases_or_lapses:
                for p in purchases_or_lapses[-2:]:
                    p_dt = parse_ts(p.get("received_at"))
                    p_fresh = round((current_time - p_dt).total_seconds(), 1) if p_dt else 0.0
                    evidence.append({"ref": f"event:{p['event_id']}", "freshness_s": p_fresh})
            signup_events = [e for e in events_so_far if e.get("event") == "signup_completed"]
            if signup_events:
                sgn = signup_events[0]
                sgn_dt = parse_ts(sgn.get("received_at"))
                s_fresh = round((current_time - sgn_dt).total_seconds(), 1) if sgn_dt else 0.0
                evidence.append({"ref": f"event:{sgn['event_id']}", "freshness_s": s_fresh})
            return GoalEvaluationResult(
                goal_id, True, "never_paid_subscriber",
                evidence,
                ["Assumes user is ready for paid conversion proposition."],
                window_hours, success_signal, condition, rule_ver
            )

        # G06: reactivate_lapsed_14d
        if goal_id == "G06":
            if status != "lapsed":
                return GoalEvaluationResult(goal_id, False, f"status_is_{status}_not_lapsed", [], [], window_hours, success_signal, condition, rule_ver)
            lapses = [e for e in events_so_far if e.get("event") == "subscription_lapsed"]
            evidence = []
            if lapses:
                last_l = lapses[-1]
                l_dt = parse_ts(last_l.get("received_at"))
                l_fresh = round((current_time - l_dt).total_seconds(), 1) if l_dt else 0.0
                evidence.append({"ref": f"event:{last_l['event_id']}", "freshness_s": l_fresh})
            else:
                f_s = round((current_time - parse_ts(status_obs)).total_seconds(), 1) if parse_ts(status_obs) else 0.0
                evidence.append({"ref": f"fact:status:{status_obs}", "freshness_s": f_s})
            return GoalEvaluationResult(
                goal_id, True, "recently_lapsed_subscriber",
                evidence,
                ["Assumes lapsed viewer has interest in new catalogue additions."],
                window_hours, success_signal, condition, rule_ver
            )

        # G07: resume_series_3d (Underspecified rule interpretation)
        if goal_id == "G07":
            min_inactive_days = UNDERSPECIFIED_GOAL_CONFIG["G07_min_days_inactive"]
            slug_plays = defaultdict(list)
            for p in plays:
                tid = p.get("properties", {}).get("title_id")
                t_obj = self.catalogue.titles.get(tid)
                if t_obj and t_obj["content_type"] in ("show", "micro_drama"):
                    slug_plays[t_obj["content_slug"]].append(p)

            candidate_slug = None
            evidence_events = []
            for slug, s_plays in slug_plays.items():
                last_play = s_plays[-1]
                last_play_time = parse_ts(last_play.get("received_at"))
                days_inactive = (current_time - last_play_time).total_seconds() / 86400.0
                if days_inactive >= min_inactive_days:
                    total_eps = len(self.episodes.get_episodes(slug))
                    completed_for_slug = {c.get("properties", {}).get("episode_id") for c in completions if self.episodes.episode_to_slug.get(c.get("properties", {}).get("episode_id")) == slug}
                    if len(completed_for_slug) < total_eps:
                        candidate_slug = slug
                        p_fresh = round((current_time - last_play_time).total_seconds(), 1)
                        evidence_events.append({"ref": f"event:{last_play['event_id']}", "freshness_s": p_fresh})
                        # Episode row
                        for ep in self.episodes.get_episodes(slug):
                            if ep["episode_id"] not in completed_for_slug:
                                evidence_events.append({"ref": f"episode:{ep['episode_id']}", "freshness_s": 0.0})
                                break
                        # Catalogue title row
                        matching_tids = self.catalogue.slug_to_titles.get(slug, [])
                        if matching_tids:
                            evidence_events.append({"ref": f"catalogue:{matching_tids[0]}", "freshness_s": 0.0})
                        break

            if candidate_slug:
                return GoalEvaluationResult(
                    goal_id, True, "unfinished_series_inactive_3d",
                    evidence_events,
                    [f"Interprets G07: unwatched episodes exist for slug {candidate_slug} and inactive for >= 3 days."],
                    window_hours, success_signal, f"content_slug:{candidate_slug}", rule_ver
                )
            return GoalEvaluationResult(goal_id, False, "no_qualifying_unfinished_series", [], [], window_hours, success_signal, condition, rule_ver)

        # G08: try_new_release_7d
        if goal_id == "G08":
            if status != "active":
                return GoalEvaluationResult(goal_id, False, f"status_is_{status}_not_active", [], [], window_hours, success_signal, condition, rule_ver)
            f_s = round((current_time - parse_ts(status_obs)).total_seconds(), 1) if parse_ts(status_obs) else 0.0
            return GoalEvaluationResult(
                goal_id, True, "active_subscriber_new_release",
                [{"ref": f"fact:status:{status_obs}", "freshness_s": f_s}],
                ["Interprets G08: active subscriber eligible to discover titles released in last 30 days."],
                window_hours, success_signal, condition, rule_ver
            )

        # G09: weekly_habit_14d
        if goal_id == "G09":
            if status != "active":
                return GoalEvaluationResult(goal_id, False, f"status_is_{status}_not_active", [], [], window_hours, success_signal, condition, rule_ver)
            fourteen_days_ago = current_time - timedelta(days=14)
            recent_play_days = {get_ist_day(parse_ts(p["received_at"])) for p in plays if parse_ts(p["received_at"]) >= fourteen_days_ago}
            if len(recent_play_days) <= UNDERSPECIFIED_GOAL_CONFIG["G09_max_active_days"]:
                f_s = round((current_time - parse_ts(status_obs)).total_seconds(), 1) if parse_ts(status_obs) else 0.0
                return GoalEvaluationResult(
                    goal_id, True, "active_sub_plays_at_most_1_day_in_14d",
                    [{"ref": f"fact:status:{status_obs}", "freshness_s": f_s}],
                    ["Interprets G09: subscriber watched <= 1 day in last 14 days."],
                    window_hours, success_signal, condition, rule_ver
                )
            return GoalEvaluationResult(goal_id, False, f"plays_on_{len(recent_play_days)}_days_in_last_14d", [], [], window_hours, success_signal, condition, rule_ver)

        # G11: tv_activation_14d
        if goal_id == "G11":
            if status != "active":
                return GoalEvaluationResult(goal_id, False, f"status_is_{status}_not_active", [], [], window_hours, success_signal, condition, rule_ver)
            f_s = round((current_time - parse_ts(status_obs)).total_seconds(), 1) if parse_ts(status_obs) else 0.0
            return GoalEvaluationResult(
                goal_id, True, "active_sub_tv_activation",
                [{"ref": f"fact:status:{status_obs}", "freshness_s": f_s}],
                ["Assumes active subscriber has potential for big screen TV activation."],
                window_hours, success_signal, condition, rule_ver
            )

        # G12: return_after_absence_7d
        if goal_id == "G12":
            fourteen_days_ago = current_time - timedelta(days=14)
            opens_in_prior_14d = [e for e in events_so_far[:-1] if e.get("event") == "app_open" and parse_ts(e.get("received_at")) >= fourteen_days_ago]
            if len(opens_in_prior_14d) == 0 and len(events_so_far) > 1:
                last_ev = events_so_far[-1]
                f_s = round((current_time - parse_ts(last_ev["received_at"])).total_seconds(), 1) if parse_ts(last_ev.get("received_at")) else 0.0
                return GoalEvaluationResult(
                    goal_id, True, "no_app_open_in_prior_14d",
                    [{"ref": f"event:{last_ev['event_id']}", "freshness_s": f_s}],
                    ["Interprets G12: viewer returned after at least 14 days of app absence."],
                    window_hours, success_signal, condition, rule_ver
                )
            return GoalEvaluationResult(goal_id, False, "not_absent_for_14d", [], [], window_hours, success_signal, condition, rule_ver)

        return GoalEvaluationResult(goal_id, False, "no_matching_rule", [], [], window_hours, success_signal, condition, rule_ver)

    def pick_best_goal(self, eligible_results: List[GoalEvaluationResult]) -> Optional[GoalEvaluationResult]:
        """Lifecycle hierarchy for picking the single best goal among eligible options."""
        # Hierarchy: G04 (trial) > G06 (lapsed) > G01 (first play) > G03 (second day return)
        # > G02 (first complete) > G07 (resume series) > G05 (first paid) > G09 (weekly habit) > G08 (new release) > G11 > G12
        priority = {
            "G04": 1,
            "G06": 2,
            "G01": 3,
            "G03": 4,
            "G02": 5,
            "G07": 6,
            "G05": 7,
            "G09": 8,
            "G08": 9,
            "G11": 10,
            "G12": 11,
        }
        valid = [r for r in eligible_results if r.eligible]
        if not valid:
            return None
        valid.sort(key=lambda r: priority.get(r.goal_id, 99))
        return valid[0]
