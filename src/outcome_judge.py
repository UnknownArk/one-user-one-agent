"""Outcome judge: Evaluates open goals after simulation using attributable events within active windows."""
import csv
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

from src.config import parse_ts, iso_ts, get_ist_day

logger = logging.getLogger("stage.outcome_judge")

class OutcomeJudge:
    def __init__(self, state_store, catalogue, episodes, arms: Dict[str, str]):
        self.state_store = state_store
        self.catalogue = catalogue
        self.episodes = episodes
        self.arms = arms

    def judge_outcomes(self, outcome_events_path: str, out_path: str) -> List[Dict[str, Any]]:
        """Reads outcome_events.jsonl from simulator and evaluates all viewer goals."""
        # Index outcome events by user_id
        user_outcome_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if os.path.exists(outcome_events_path):
            with open(outcome_events_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ev = json.loads(line)
                        user_outcome_events[ev["user_id"]].append(ev)

        # Retrieve all goals from state store
        cur = self.state_store.conn.cursor()
        cur.execute("SELECT * FROM viewer_goals ORDER BY started_at ASC")
        all_goals = [dict(r) for r in cur.fetchall()]

        judged_records = []

        for g in all_goals:
            uid = g["user_id"]
            gid = g["goal_id"]
            g_inst_id = g["goal_instance_id"]
            start_dt = parse_ts(g["started_at"])
            deadline_dt = parse_ts(g["deadline_at"])
            rule_ver = g["rule_version"]
            signal = g["success_signal"]
            cond_data = json.loads(g["success_condition_json"]) if g.get("success_condition_json") else {}
            arm = self.arms.get(uid, "agent")

            user_events = user_outcome_events.get(uid, [])
            # Also combine with any replay events recorded in store after goal start
            store_events = self.state_store.get_events_for_user(uid)
            combined_events = user_events + [e for e in store_events if e not in user_events]
            combined_events.sort(key=lambda x: x.get("received_at", ""))

            # Filter events within goal window (start_dt <= received_at <= deadline_dt)
            window_events = [
                e for e in combined_events
                if parse_ts(e.get("received_at")) and start_dt <= parse_ts(e.get("received_at")) <= deadline_dt
            ]

            # Check for exposure (home_page_shown personalised or message_delivered)
            exposures = [
                e for e in window_events
                if (e.get("event") == "home_page_shown" and e.get("properties", {}).get("personalised"))
                or e.get("event") == "message_delivered"
            ]
            has_exposure = len(exposures) > 0

            # Judge goal success based on goal definition and rule_version
            succeeded = False
            attributable_eid = None
            reason = ""

            for e in window_events:
                ev_name = e.get("event")
                r_dt = parse_ts(e.get("received_at"))
                props = e.get("properties", {})

                # G01: first_play_24h
                if gid == "G01" and ev_name == "play_started":
                    succeeded = True
                    attributable_eid = e.get("event_id")
                    reason = f"First play started on title {props.get('title_id')} at {iso_ts(r_dt)}"
                    break

                # G02: first_full_episode_3d
                elif gid == "G02" and ev_name == "episode_completed":
                    succeeded = True
                    attributable_eid = e.get("event_id")
                    reason = f"First episode completed for title {props.get('title_id')} at {iso_ts(r_dt)}"
                    break

                # G03: second_day_return_2d
                elif gid == "G03":
                    # Check rule version in force
                    if "corrected" in rule_ver:
                        # Corrected rule requires play_started on later IST calendar day
                        if ev_name == "play_started" and get_ist_day(r_dt) > get_ist_day(start_dt):
                            succeeded = True
                            attributable_eid = e.get("event_id")
                            reason = f"Play started on later IST calendar day {get_ist_day(r_dt)} (corrected rule)"
                            break
                    else:
                        # Original rule accepted app_open on later IST calendar day
                        if ev_name == "app_open" and get_ist_day(r_dt) > get_ist_day(start_dt):
                            succeeded = True
                            attributable_eid = e.get("event_id")
                            reason = f"App opened on later IST calendar day {get_ist_day(r_dt)} (original rule)"
                            break

                # G04: trial_to_paid_7d & G05: first_paid_7d
                elif gid in ("G04", "G05") and ev_name == "purchase_completed":
                    if not props.get("is_trial", False):
                        succeeded = True
                        attributable_eid = e.get("event_id")
                        reason = f"Purchased paid plan {props.get('plan_id')} (price ₹{props.get('price_inr')})"
                        break

                # G06: reactivate_lapsed_14d
                elif gid == "G06" and ev_name in ("purchase_completed", "subscription_renewed"):
                    succeeded = True
                    attributable_eid = e.get("event_id")
                    reason = f"Reactivated subscription via {ev_name} at {iso_ts(r_dt)}"
                    break

                # G07: resume_series_3d
                elif gid == "G07" and ev_name == "play_started":
                    target_slug = cond_data.get("condition", "").replace("content_slug:", "")
                    tid = props.get("title_id")
                    t_obj = self.catalogue.titles.get(tid)
                    if t_obj and t_obj.get("content_slug") == target_slug:
                        succeeded = True
                        attributable_eid = e.get("event_id")
                        reason = f"Resumed series slug {target_slug} on title {tid}"
                        break

                # G08: try_new_release_7d
                elif gid == "G08" and ev_name == "play_started":
                    tid = props.get("title_id")
                    t_obj = self.catalogue.titles.get(tid)
                    if t_obj and t_obj.get("release_date"):
                        succeeded = True
                        attributable_eid = e.get("event_id")
                        reason = f"Played new release {tid} ({t_obj.get('title')})"
                        break

                # G09: weekly_habit_14d
                elif gid == "G09" and ev_name == "play_started":
                    # Check if plays occurred on 3 distinct IST days in window
                    play_days = {get_ist_day(parse_ts(p["received_at"])) for p in window_events if p.get("event") == "play_started"}
                    if len(play_days) >= 3:
                        succeeded = True
                        attributable_eid = e.get("event_id")
                        reason = f"Built weekly habit across {len(play_days)} distinct IST days"
                        break

                # G11: tv_activation_14d
                elif gid == "G11" and ev_name == "tv_app_open":
                    succeeded = True
                    attributable_eid = e.get("event_id")
                    reason = f"Activated on TV app at {iso_ts(r_dt)}"
                    break

                # G12: return_after_absence_7d
                elif gid == "G12" and ev_name == "play_started":
                    succeeded = True
                    attributable_eid = e.get("event_id")
                    reason = f"Returned and played title {props.get('title_id')}"
                    break

            judged_at = iso_ts(datetime.now())
            if succeeded:
                final_status = "SUCCEEDED"
                lesson = f"Goal {gid} succeeded: {reason} (exposure: {has_exposure})."
            else:
                final_status = "FAILED"
                reason = f"No attributable success signal before deadline {iso_ts(deadline_dt)}"
                lesson = f"Goal {gid} failed: {reason}."

            # Update state store
            self.state_store.close_goal(g_inst_id, final_status, judged_at, reason)
            self.state_store.record_lesson(uid, lesson, judged_at)

            record = {
                "goal_instance_id": g_inst_id,
                "user_id": uid,
                "arm": arm,
                "goal_id": gid,
                "status": final_status,
                "rule_version": rule_ver,
                "started_at": g["started_at"],
                "deadline_at": g["deadline_at"],
                "judged_at": judged_at,
                "judgment_reason": reason,
                "attributable_event_id": attributable_eid,
                "has_exposure": has_exposure
            }
            judged_records.append(record)

        # Write outcomes.jsonl
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for rec in judged_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        logger.info("Judged %d goals: %d succeeded, %d failed",
                    len(judged_records),
                    sum(1 for r in judged_records if r["status"] == "SUCCEEDED"),
                    sum(1 for r in judged_records if r["status"] == "FAILED"))
        return judged_records
