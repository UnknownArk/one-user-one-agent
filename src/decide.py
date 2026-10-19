"""Decision engine implementing the 7-step loop with provenance, budgets, abstention, and verifier integration."""
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple, Set

from src.config import (
    parse_ts, iso_ts, get_ist_day, is_quiet_hours_ist, next_morning_ist,
    STALE_TRIGGER_THRESHOLD_SECONDS, MAX_MODEL_CALLS, MAX_TOOL_CALLS,
    MAX_HOME_ROWS, MIN_ROW_TITLES, MAX_ROW_TITLES, APPROVED_WA_TEMPLATES, VALID_PLANS,
    DecisionRecord, ActionRecord
)
from src.model_provider import ModelTimeoutException, ModelMalformedOutputException, ModelUnavailable503Exception

logger = logging.getLogger("stage.decide")

class DecisionEngine:
    def __init__(self, data_loader, state_store, goal_planner, model_provider, verifier):
        self.loader = data_loader
        self.catalogue = data_loader.catalogue
        self.episodes = data_loader.episodes
        self.state_store = state_store
        self.goal_planner = goal_planner
        self.model_provider = model_provider
        self.verifier = verifier

        # Decision counters for sequential ID generation
        self.decision_counter = 0
        self.action_counter = 0

    def process_trigger(self, trigger_event: Dict[str, Any], deliver_at: datetime) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Processes one trigger event within strict latency budget.
        
        Returns:
            (decision_dict, list_of_action_dicts)
        """
        start_mono = time.monotonic()
        self.decision_counter += 1
        decision_id = f"D-{self.decision_counter:04d}"

        uid = trigger_event["user_id"]
        trigger_eid = trigger_event["event_id"]
        ev_name = trigger_event.get("event", "app_open")
        received_at_dt = parse_ts(trigger_event.get("received_at")) or deliver_at
        occurred_at_dt = parse_ts(trigger_event.get("occurred_at")) or received_at_dt

        arm = self.verifier.arms.get(uid, "agent")
        is_holdout = (arm == "holdout")

        # Idempotency check: duplicate triggers return previous decision with no duplicate actions
        prior_dec = self.state_store.get_decision_by_trigger(trigger_eid)
        if prior_dec:
            logger.info("Trigger %s already processed (idempotency hit). Returning cached decision.", trigger_eid)
            return prior_dec, []

        # Record trigger event into state store
        self.state_store.record_event(trigger_event)

        # 1. Stale Trigger & Clock Skew checks
        event_age_s = (deliver_at - occurred_at_dt).total_seconds()
        clock_skew = event_age_s < 0

        assumptions = []
        if clock_skew:
            assumptions.append(f"Phone clock ahead of server by {abs(event_age_s):.1f}s (clock skew).")
            event_age_s = 0.0

        trigger_info = {
            "event": ev_name,
            "received_at": iso_ts(received_at_dt),
            "event_age_s": round(event_age_s, 1),
            "trigger_event_id": trigger_eid
        }

        # Stale trigger abstention (offline-buffered bursts must not produce messages or live pages)
        if event_age_s > STALE_TRIGGER_THRESHOLD_SECONDS:
            elapsed_ms = int((time.monotonic() - start_mono) * 1000)
            decision = {
                "decision_id": decision_id,
                "viewer_id": uid,
                "arm": arm,
                "suppressed": False,
                "would_have_action": None,
                "trigger": trigger_info,
                "goal": None,
                "action": "NO_ACTION",
                "dialect": {"used": trigger_event.get("app_dialect", "d1"), "source": "app_setting", "conflicts": []},
                "content": [],
                "confidence": 0.99,
                "confidence_reason": f"Trigger event is {event_age_s:.0f}s old (> 600s threshold); offline-buffered event ignored.",
                "evidence": [{"ref": f"event:{trigger_eid}", "freshness_s": round(max(0.0, event_age_s), 1)}],
                "assumptions": assumptions + ["Offline-buffered historical app opens do not reflect a live user session."],
                "checks": {"passed": ["stale_trigger_check"], "failed": []},
                "decided_in_ms": max(1, elapsed_ms),
                "status": "ABSTAINED",
                "no_action_reason": "stale_trigger",
                "model_calls": 0,
                "tool_calls": 0,
                "fallback_used": False
            }
            self.state_store.record_decision(decision)
            return decision, []

        # 2. Dialect Resolution with Precedence and Conflict Auditing
        app_dialect = trigger_event.get("app_dialect", "d1")
        dialect_conflicts = []

        # Check profile preferred dialect
        prof_fact = self.state_store.get_fact(uid, "preferred_dialect")
        if prof_fact and prof_fact.value != app_dialect:
            dialect_conflicts.append(f"profile:{prof_fact.value}")

        # Check campaign deeplink dialect if present
        deeplink = trigger_event.get("properties", {}).get("deeplink", "")
        if "ott://c/" in deeplink:
            parts = deeplink.split("/")
            if len(parts) >= 5:
                dl_dialect = parts[4]
                if dl_dialect != app_dialect:
                    dialect_conflicts.append(f"deeplink:{dl_dialect}")

        dialect_info = {
            "used": app_dialect,
            "source": "app_setting",
            "conflicts": dialect_conflicts
        }

        # 3. Assemble viewer facts and history
        viewer_facts = self.state_store.get_all_facts(uid)
        user_events = self.state_store.get_events_for_user(uid, up_to_ts=iso_ts(deliver_at))

        # Update viewer facts from recent events
        status_fact = viewer_facts.get("subscription_status")
        status = status_fact.value if status_fact else "never"

        # Check for mid-series situation changes (e.g. user just purchased)
        recent_purchases = [e for e in user_events if e.get("event") == "purchase_completed"]
        has_new_purchase = False
        if recent_purchases:
            last_p_time = parse_ts(recent_purchases[-1]["received_at"])
            if (deliver_at - last_p_time).total_seconds() < 300: # Within last 5 mins
                has_new_purchase = True

        # Check active goal
        active_goal = self.state_store.get_active_goal(uid)
        if active_goal and has_new_purchase and active_goal.get("goal_id") in ("G04", "G05"):
            # Situation change: user bought, so close first_paid/trial goal as succeeded
            self.state_store.close_goal(active_goal["goal_instance_id"], "SUCCEEDED", iso_ts(deliver_at), "purchase_completed_during_replay")
            self.state_store.cancel_pending_actions(uid, iso_ts(deliver_at), "user_purchased_subscription")
            self.state_store.record_lesson(uid, f"User converted to paid subscription during goal {active_goal['goal_id']}; pending messages cancelled.", iso_ts(deliver_at))
            active_goal = None

        # 4. Model Fault Check: Handle injected unsupported_claim from model_faults.jsonl
        model_calls = 0
        tool_calls = 1
        is_503 = getattr(self.model_provider, "is_service_503", False)
        fault_claim = None
        if not is_503:
            fault_claim = self.model_provider.query_claim_fault(trigger_eid)
            model_calls += 1
        else:
            assumptions.append("Model dependency unavailable (HTTP 503 control window); rules-only mode active.")

        selected_goal_res = None
        evidence_list = [{"ref": f"event:{trigger_eid}", "freshness_s": round(max(0.0, event_age_s), 1)}]

        if fault_claim:
            # Model hallucinated eligibility without evidence!
            claimed_gid = fault_claim.get("goal_id", "G04")
            logger.info("Model proposed claim for %s with reason: %s", claimed_gid, fault_claim.get("reason"))
            # Submit to verifier
            temp_dec = {
                "viewer_id": uid,
                "goal": {"id": claimed_gid},
                "evidence": [{"ref": f"event:{trigger_eid}", "freshness_s": round(max(0.0, event_age_s), 1)}],
                "dialect": dialect_info,
                "action": "NO_ACTION"
            }
            claim_ver_res = self.verifier.verify_decision(temp_dec, deliver_at)
            if not claim_ver_res.is_valid:
                # Verifier correctly caught and vetoed unsupported model claim!
                logger.info("Verifier VETOED unsupported model claim: %s", claim_ver_res.reasons)
                assumptions.append("Model claimed goal eligibility without event store evidence; vetoed by verifier.")
                # Fall back to rule-based goal selection

        # 5. Goal Planning
        if not active_goal:
            eligible_goals = self.goal_planner.evaluate_all_goals(uid, deliver_at, viewer_facts, user_events)
            selected_goal_res = self.goal_planner.pick_best_goal(eligible_goals)
            if selected_goal_res:
                deadline_dt = deliver_at + timedelta(hours=selected_goal_res.window_hours)
                goal_inst_id = self.state_store.open_goal(
                    user_id=uid,
                    goal_id=selected_goal_res.goal_id,
                    started_at=iso_ts(deliver_at),
                    deadline_at=iso_ts(deadline_dt),
                    success_signal=selected_goal_res.success_signal or "play_started",
                    success_condition={"condition": selected_goal_res.condition},
                    rule_version=selected_goal_res.rule_version
                )
                evidence_list.extend(selected_goal_res.evidence)
                assumptions.extend(selected_goal_res.assumptions)
                goal_dict = {
                    "id": selected_goal_res.goal_id,
                    "deadline": iso_ts(deadline_dt),
                    "signal": selected_goal_res.success_signal,
                    "rule_version": selected_goal_res.rule_version
                }
            else:
                goal_dict = None
        else:
            goal_start_dt = parse_ts(active_goal.get("started_at"))
            goal_fresh = round((deliver_at - goal_start_dt).total_seconds(), 1) if goal_start_dt else 0.0
            evidence_list.append({"ref": f"goal:{active_goal['goal_instance_id']}", "freshness_s": goal_fresh})
            goal_dict = {
                "id": active_goal["goal_id"],
                "deadline": active_goal["deadline_at"],
                "signal": active_goal["success_signal"],
                "rule_version": active_goal["rule_version"]
            }

        # 6. Candidate Content Selection & Model Ranking
        # Compute user's genre preferences and history
        plays = [e for e in user_events if e.get("event") == "play_started"]
        genre_counts = defaultdict(int)
        for p in plays:
            tid = p.get("properties", {}).get("title_id")
            t_obj = self.catalogue.titles.get(tid)
            if t_obj:
                for g in t_obj.get("genres", []):
                    genre_counts[g] += 1
        top_genre = max(genre_counts, key=genre_counts.get) if genre_counts else None

        # Filter available titles in active dialect
        dialect_titles = [
            tid for tid in self.catalogue.titles_by_dialect.get(app_dialect, [])
            if self.catalogue.is_available(tid, deliver_at)
        ]

        if not dialect_titles:
            # No content available in this dialect
            elapsed_ms = int((time.monotonic() - start_mono) * 1000)
            decision = {
                "decision_id": decision_id,
                "viewer_id": uid,
                "arm": arm,
                "suppressed": False,
                "would_have_action": None,
                "trigger": trigger_info,
                "goal": goal_dict,
                "action": "NO_ACTION",
                "dialect": dialect_info,
                "content": [],
                "confidence": 0.95,
                "confidence_reason": f"No active titles available for dialect {app_dialect} at this time.",
                "evidence": evidence_list,
                "assumptions": assumptions,
                "checks": {"passed": ["dialect_check"], "failed": []},
                "decided_in_ms": max(1, elapsed_ms),
                "status": "ABSTAINED",
                "no_action_reason": "insufficient_evidence",
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "fallback_used": False
            }
            self.state_store.record_decision(decision)
            return decision, []

        # Model ranking with fallback
        fallback_used = is_503
        user_ctx = {"top_genre": top_genre, "catalogue": self.catalogue.titles}
        ranked_titles = dialect_titles
        if is_503:
            ranked_titles = sorted(dialect_titles, key=lambda t: (1 if top_genre in self.catalogue.titles.get(t, {}).get("genres", []) else 0), reverse=True)
        else:
            try:
                if model_calls < MAX_MODEL_CALLS:
                    ranked_titles = self.model_provider.rank_candidates(dialect_titles, user_ctx, trigger_eid)
                    model_calls += 1
            except (ModelTimeoutException, ModelMalformedOutputException, ModelUnavailable503Exception) as e:
                logger.warning("Model failure (%s); falling back to deterministic heuristic ranking.", type(e).__name__)
                fallback_used = True
                assumptions.append(f"Model dependency degraded/failed ({type(e).__name__}); rule-based ranking fallback used.")
                # Deterministic fallback ranking
                ranked_titles = sorted(dialect_titles, key=lambda t: (1 if top_genre in self.catalogue.titles.get(t, {}).get("genres", []) else 0), reverse=True)

        # 7. Construct Home Page Rows (3-10 titles/row, max 6 rows, strictly 1 entry per content_slug)
        seen_slugs: Set[str] = set()
        hero_title_id = ranked_titles[0] if ranked_titles else None
        if hero_title_id:
            seen_slugs.add(self.catalogue.titles[hero_title_id]["content_slug"])

        home_rows = []

        # Row 1: popular_in_dialect
        pop_row_tids = []
        for tid in ranked_titles:
            t_obj = self.catalogue.titles.get(tid)
            if t_obj and t_obj["content_slug"] not in seen_slugs:
                seen_slugs.add(t_obj["content_slug"])
                pop_row_tids.append(tid)
                if len(pop_row_tids) >= 6:
                    break
        if len(pop_row_tids) >= MIN_ROW_TITLES:
            home_rows.append({"row_type": "popular_in_dialect", "title_ids": pop_row_tids})

        # Row 2: because_you_watched (if top_genre exists)
        if top_genre:
            genre_tids = []
            for tid in ranked_titles:
                t_obj = self.catalogue.titles.get(tid)
                if t_obj and top_genre in t_obj.get("genres", []) and t_obj["content_slug"] not in seen_slugs:
                    seen_slugs.add(t_obj["content_slug"])
                    genre_tids.append(tid)
                    if len(genre_tids) >= 6:
                        break
            if len(genre_tids) >= MIN_ROW_TITLES:
                home_rows.append({"row_type": "because_you_watched", "title_ids": genre_tids})

        # Row 3: new_releases
        new_rel_tids = []
        for tid in sorted(ranked_titles, key=lambda t: self.catalogue.titles.get(t, {}).get("release_date") or "", reverse=True):
            t_obj = self.catalogue.titles.get(tid)
            if t_obj and t_obj["content_slug"] not in seen_slugs:
                seen_slugs.add(t_obj["content_slug"])
                new_rel_tids.append(tid)
                if len(new_rel_tids) >= 6:
                    break
        if len(new_rel_tids) >= MIN_ROW_TITLES:
            home_rows.append({"row_type": "new_releases", "title_ids": new_rel_tids})

        # Row 4: genre (fallback general row if room)
        other_tids = []
        for tid in ranked_titles:
            t_obj = self.catalogue.titles.get(tid)
            if t_obj and t_obj["content_slug"] not in seen_slugs:
                seen_slugs.add(t_obj["content_slug"])
                other_tids.append(tid)
                if len(other_tids) >= 6:
                    break
        if len(other_tids) >= MIN_ROW_TITLES and len(home_rows) < MAX_HOME_ROWS:
            home_rows.append({"row_type": "genre", "title_ids": other_tids})

        # 8. Check if Holdout Arm
        arm = self.verifier.arms.get(uid, "agent")
        is_holdout = (arm == "holdout")

        # 9. Final Decision & Actions Formulation
        published_actions = []
        featured_content = [f"title:{hero_title_id}"] if hero_title_id else []
        for r in home_rows:
            for tid in r["title_ids"][:3]:
                ref = f"title:{tid}"
                if ref not in featured_content:
                    featured_content.append(ref)

        elapsed_ms = int((time.monotonic() - start_mono) * 1000)
        published_at_dt = deliver_at + timedelta(milliseconds=elapsed_ms)

        if not home_rows and not hero_title_id:
            # Cannot build valid page
            decision = {
                "decision_id": decision_id,
                "viewer_id": uid,
                "arm": arm,
                "suppressed": False,
                "would_have_action": None,
                "trigger": trigger_info,
                "goal": goal_dict,
                "action": "NO_ACTION",
                "dialect": dialect_info,
                "content": [],
                "confidence": 0.85,
                "confidence_reason": "Not enough valid content to construct minimum 3-title rows.",
                "evidence": evidence_list,
                "assumptions": assumptions,
                "checks": {"passed": [], "failed": ["row_minimum_titles"]},
                "decided_in_ms": max(1, elapsed_ms),
                "status": "ABSTAINED",
                "no_action_reason": "insufficient_evidence",
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "fallback_used": fallback_used
            }
            self.state_store.record_decision(decision)
            return decision, []

        # Build proposed HOME_PAGE action
        self.action_counter += 1
        page_action_id = f"A-{self.action_counter:04d}"
        page_action = {
            "action_id": page_action_id,
            "decision_id": decision_id,
            "user_id": uid,
            "trigger_event_id": trigger_eid,
            "type": "home_page",
            "published_at": iso_ts(published_at_dt),
            "dialect": app_dialect,
            "hero_title_id": hero_title_id,
            "rows": home_rows
        }

        # Run independent verification
        status = "PUBLISHED"
        passed_checks = []
        failed_checks = []

        dec_verify_res = self.verifier.verify_decision({
            "viewer_id": uid,
            "goal": goal_dict,
            "action": "HOME_PAGE" if not is_holdout else "NO_ACTION",
            "dialect": dialect_info,
            "evidence": evidence_list,
            "status": "HOLDOUT_SUPPRESSED" if is_holdout else "PUBLISHED"
        }, published_at_dt)
        passed_checks.extend(dec_verify_res.passed_checks)
        failed_checks.extend(dec_verify_res.failed_checks)

        act_verify_res = self.verifier.verify_action(page_action, viewer_facts, published_at_dt)
        passed_checks.extend(act_verify_res.passed_checks)
        failed_checks.extend(act_verify_res.failed_checks)

        if is_holdout:
            status = "HOLDOUT_SUPPRESSED"
            no_act_reason = "holdout"
            failed_checks = [c for c in failed_checks if c != "holdout_veto"]
        elif not act_verify_res.is_valid:
            if "duplicate_action" in act_verify_res.failed_checks:
                status = "VETOED"
                no_act_reason = "duplicate_action"
            else:
                status = "REJECTED"
                no_act_reason = "verification_failed"
        else:
            status = "PUBLISHED"
            no_act_reason = None
            published_actions.append(page_action)
            self.state_store.record_action(page_action)

        # 10. Narrow Message Path (Agent arm only)
        msg_decision = None
        if not is_holdout and status in ("PUBLISHED", "VETOED"):
            if goal_dict and goal_dict.get("id") in ("G04", "G05", "G06", "G07"):
                push_fact = viewer_facts.get("notifications_opt_in")
                wa_fact = viewer_facts.get("whatsapp_opt_in")
                has_push = bool(push_fact and push_fact.value is True)
                has_wa = bool(wa_fact and wa_fact.value is True)

                # Determine send time
                if is_quiet_hours_ist(deliver_at):
                    send_at_dt = next_morning_ist(deliver_at)
                else:
                    send_at_dt = deliver_at + timedelta(seconds=15)

                ch = "push" if has_push else ("whatsapp" if has_wa else "push")

                candidate_tid = hero_title_id
                if candidate_tid:
                    t_cand = self.catalogue.titles.get(candidate_tid)
                    if not t_cand or t_cand.get("dialect") != app_dialect or not self.catalogue.is_available(candidate_tid, send_at_dt):
                        candidate_tid = None
                if not candidate_tid:
                    for tid in dialect_titles:
                        if self.catalogue.is_available(tid, send_at_dt):
                            candidate_tid = tid
                            break

                if candidate_tid:
                    t_obj = self.catalogue.titles.get(candidate_tid)
                    t_title = t_obj.get("title", "")
                    self.action_counter += 1
                    msg_action_id = f"A-{self.action_counter:04d}"
                    if ch == "push":
                        msg_action = {
                            "action_id": msg_action_id,
                            "decision_id": decision_id,
                            "user_id": uid,
                            "trigger_event_id": trigger_eid,
                            "type": "push",
                            "published_at": iso_ts(published_at_dt),
                            "send_at": iso_ts(send_at_dt),
                            "title_id": candidate_tid,
                            "dialect": app_dialect,
                            "goal_id": goal_dict["id"],
                            "text": {
                                "title": f"Recommended in {app_dialect}"[:40],
                                "body": f"Watch {t_title} now on STAGE."[:120]
                            },
                            "deeplink": f"ott://title/{candidate_tid}"
                        }
                    else:
                        msg_action = {
                            "action_id": msg_action_id,
                            "decision_id": decision_id,
                            "user_id": uid,
                            "trigger_event_id": trigger_eid,
                            "type": "whatsapp",
                            "published_at": iso_ts(published_at_dt),
                            "send_at": iso_ts(send_at_dt),
                            "title_id": candidate_tid,
                            "dialect": app_dialect,
                            "goal_id": goal_dict["id"],
                            "template_id": "wa_recommend_title_v1",
                            "variables": {
                                "title_name": t_title,
                                "dialect_name": app_dialect
                            }
                        }

                    msg_res = self.verifier.verify_action(msg_action, viewer_facts, published_at_dt)
                    passed_checks.extend(msg_res.passed_checks)
                    failed_checks.extend(msg_res.failed_checks)
                    if msg_res.is_valid:
                        published_actions.append(msg_action)
                        self.state_store.record_action(msg_action)
                        msg_decision = {"status": "PUBLISHED", "action_id": msg_action_id, "channel": ch}
                    else:
                        veto_r = msg_res.failed_checks[0] if msg_res.failed_checks else "verification_failed"
                        msg_decision = {"status": "VETOED", "channel": ch, "reason": veto_r}
                else:
                    msg_decision = {"status": "VETOED", "channel": ch, "reason": "no_available_title"}
            else:
                msg_decision = {"status": "VETOED", "channel": "none", "reason": "ineligible_goal"}

        decision = {
            "decision_id": decision_id,
            "viewer_id": uid,
            "arm": arm,
            "suppressed": is_holdout,
            "would_have_action": "HOME_PAGE" if is_holdout else None,
            "trigger": trigger_info,
            "goal": goal_dict,
            "action": "HOME_PAGE" if status == "PUBLISHED" else "NO_ACTION",
            "message_decision": msg_decision,
            "dialect": dialect_info,
            "content": featured_content[:5],
            "confidence": 0.88 if not fallback_used else 0.72,
            "confidence_reason": "Personalized layout matches viewer dialect and favorite genres." if not fallback_used else "Heuristic layout generated during model fault fallback.",
            "evidence": evidence_list,
            "assumptions": assumptions + [f"Viewer will interact with personalized rows in dialect {app_dialect}."],
            "checks": {"passed": list(set(passed_checks)), "failed": list(set(failed_checks))},
            "decided_in_ms": max(1, elapsed_ms),
            "status": status,
            "no_action_reason": no_act_reason,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "fallback_used": fallback_used
        }

        self.state_store.record_decision(decision)
        return decision, published_actions
