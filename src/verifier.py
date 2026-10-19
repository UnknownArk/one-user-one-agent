"""Independent verification engine. Acts as a strict gatekeeper with unilateral veto power."""
import logging
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Tuple, Set

from src.config import (
    parse_ts, iso_ts, get_ist_day, is_quiet_hours_ist,
    FORBIDDEN_PUSH_PHRASES, APPROVED_WA_TEMPLATES, VALID_PLANS,
    MAX_HOME_ROWS, MIN_ROW_TITLES, MAX_ROW_TITLES
)

logger = logging.getLogger("stage.verifier")

class VerificationResult:
    def __init__(self):
        self.passed_checks: List[str] = []
        self.failed_checks: List[str] = []
        self.reasons: List[str] = []

    @property
    def is_valid(self) -> bool:
        return len(self.failed_checks) == 0

    def pass_check(self, name: str):
        self.passed_checks.append(name)

    def fail_check(self, name: str, reason: str):
        self.failed_checks.append(name)
        self.reasons.append(reason)
        logger.warning("Verification check FAILED [%s]: %s", name, reason)

class PolicyVerifier:
    def __init__(self, catalogue, arms: Dict[str, str], state_store):
        self.catalogue = catalogue
        self.arms = arms
        self.state_store = state_store

    def verify_decision(self, decision: Dict[str, Any], published_at: datetime) -> VerificationResult:
        res = VerificationResult()
        uid = decision.get("viewer_id")
        action = decision.get("action")
        goal_info = decision.get("goal")

        # 1. Holdout verification
        arm = self.arms.get(uid, "agent")
        if arm == "holdout":
            if action != "NO_ACTION" and decision.get("status") != "HOLDOUT_SUPPRESSED":
                res.fail_check("holdout_compliance", f"Viewer {uid} is in holdout arm; actions must be suppressed.")
            else:
                res.pass_check("holdout_compliance")
        else:
            res.pass_check("holdout_compliance")

        # 2. Goal eligibility & Evidence grounding verification
        if goal_info:
            gid = goal_info.get("id")
            if gid == "G10":
                res.fail_check("goal_measurable", "Goal G10 is unmeasurable (null success event).")
            else:
                res.pass_check("goal_measurable")

            # Check evidence references resolve in event store
            evidence_refs = decision.get("evidence", [])
            for item in evidence_refs:
                ref = item.get("ref") if isinstance(item, dict) else str(item)
                if ref.startswith("event:"):
                    eid = ref.split(":", 1)[1]
                    if not self.state_store.is_event_seen(eid):
                        res.fail_check("evidence_grounding", f"Cited event {eid} does not exist in event store.")
                        break
            if "evidence_grounding" not in res.failed_checks:
                res.pass_check("evidence_grounding")

            # Verify subscription claims independently (e.g. model claim of G04 when not trial_active)
            if gid == "G04":
                status_fact = self.state_store.get_fact(uid, "subscription_status")
                actual_status = status_fact.value if status_fact else "never"
                if actual_status != "trial_active":
                    res.fail_check("goal_eligibility", f"Model claimed G04 (trial) for user {uid}, but verified status is '{actual_status}'.")
                else:
                    res.pass_check("goal_eligibility")
            else:
                res.pass_check("goal_eligibility")

        # 3. Dialect consistency verification
        used_dialect = decision.get("dialect", {}).get("used")
        if not used_dialect:
            res.fail_check("dialect_specified", "Dialect used was not specified in decision.")
        else:
            res.pass_check("dialect_specified")

        return res

    def verify_action(self, action: Dict[str, Any], viewer_facts: Dict[str, Any], published_at: datetime) -> VerificationResult:
        res = VerificationResult()
        uid = action.get("user_id")
        act_type = action.get("type")

        # 1. Channel-specific checks (holdout suppression is enforced in decide.py)
        if act_type == "home_page":
            self._verify_home_page(action, published_at, res)
        elif act_type in ("push", "whatsapp"):
            self._verify_messaging(action, viewer_facts, published_at, res)
        elif act_type == "in_app_message":
            res.pass_check("in_app_message_valid")
        else:
            res.fail_check("action_type_valid", f"Unknown action type: {act_type}")

        return res

    def _verify_home_page(self, action: Dict[str, Any], published_at: datetime, res: VerificationResult):
        d = action.get("dialect")
        hero_id = action.get("hero_title_id")
        rows = action.get("rows") or []

        # Duplicate page check within 120-min TTL
        uid = action.get("user_id")
        if self.state_store and uid:
            prior_actions = self.state_store.get_actions_for_user(uid)
            for pact in prior_actions:
                if pact.get("type") == "home_page":
                    p_time = parse_ts(pact.get("published_at"))
                    if p_time:
                        diff_s = (published_at - p_time).total_seconds()
                        if 0 <= diff_s <= 7200:  # 120 minutes TTL
                            if (action.get("hero_title_id") == pact.get("hero_title_id") and
                                action.get("rows") == pact.get("rows")):
                                res.fail_check("duplicate_action",
                                               f"Identical home page content already live for user {uid} within 120-min TTL.")
                                return
        res.pass_check("duplicate_action")

        # Hero title check
        if hero_id:
            hero = self.catalogue.titles.get(hero_id)
            if not hero:
                res.fail_check("hero_exists", f"Hero title {hero_id} does not exist.")
            elif hero["dialect"] != d:
                res.fail_check("hero_dialect", f"Hero title {hero_id} dialect '{hero['dialect']}' does not match page dialect '{d}'.")
            elif not self.catalogue.is_available(hero_id, published_at):
                res.fail_check("hero_available", f"Hero title {hero_id} is not available at {iso_ts(published_at)}.")
            else:
                res.pass_check("hero_valid")
        else:
            res.pass_check("hero_valid")

        # Row checks
        if len(rows) > MAX_HOME_ROWS:
            res.fail_check("max_rows", f"Home page has {len(rows)} rows; maximum allowed is {MAX_HOME_ROWS}.")
        else:
            res.pass_check("max_rows")

        seen_slugs: Set[str] = set()
        if hero_id and hero_id in self.catalogue.titles:
            seen_slugs.add(self.catalogue.titles[hero_id]["content_slug"])

        valid_rows_count = 0
        for i, row in enumerate(rows):
            tids = row.get("title_ids", [])
            row_type = row.get("row_type")
            if row_type == "continue_watching":
                res.fail_check("no_continue_watching", "Agent must never publish 'Continue Watching' row.")
                continue

            valid_row_tids = []
            for tid in tids:
                t = self.catalogue.titles.get(tid)
                if not t:
                    continue
                if t["dialect"] != d:
                    continue
                if not self.catalogue.is_available(tid, published_at):
                    continue
                if t["content_slug"] in seen_slugs:
                    continue
                seen_slugs.add(t["content_slug"])
                valid_row_tids.append(tid)

            if len(valid_row_tids) < MIN_ROW_TITLES:
                res.fail_check(f"row_{i}_min_titles", f"Row {i} has only {len(valid_row_tids)} valid titles; minimum is {MIN_ROW_TITLES}.")
            elif len(valid_row_tids) > MAX_ROW_TITLES:
                res.fail_check(f"row_{i}_max_titles", f"Row {i} has {len(valid_row_tids)} titles; maximum is {MAX_ROW_TITLES}.")
            else:
                valid_rows_count += 1

        if valid_rows_count > 0:
            res.pass_check("home_page_structure")

    def _verify_messaging(self, action: Dict[str, Any], viewer_facts: Dict[str, Any],
                           published_at: datetime, res: VerificationResult):
        act_type = action.get("type")
        uid = action.get("user_id")
        send_at_dt = parse_ts(action.get("send_at")) or published_at
        tid = action.get("title_id")

        # Goal plausibility check
        gid = action.get("goal_id")
        if gid and gid not in ("G04", "G05", "G06", "G07"):
            res.fail_check("goal_plausibility", f"Goal {gid} is not plausible for messaging (must be G04, G05, G06, or G07).")
        else:
            res.pass_check("goal_plausibility")

        # Title check if referencing content
        if tid:
            t = self.catalogue.titles.get(tid)
            app_dialect = action.get("dialect")
            if not t:
                res.fail_check("message_title_exists", f"Title {tid} referenced in message does not exist.")
            elif app_dialect and t.get("dialect") != app_dialect:
                res.fail_check("message_title_dialect", f"Title {tid} dialect '{t.get('dialect')}' does not match message dialect '{app_dialect}'.")
            elif not self.catalogue.is_available(tid, send_at_dt):
                res.fail_check("message_title_available", f"Title {tid} is not available at message send time.")
            else:
                res.pass_check("message_title_valid")

        # Consent check
        if act_type == "push":
            push_fact = viewer_facts.get("notifications_opt_in")
            if not (push_fact and push_fact.value is True):
                res.fail_check("push_consent", f"Viewer {uid} has not opted in to push notifications.")
            else:
                res.pass_check("push_consent")
        elif act_type == "whatsapp":
            wa_fact = viewer_facts.get("whatsapp_opt_in")
            if not (wa_fact and wa_fact.value is True):
                res.fail_check("whatsapp_consent", f"Viewer {uid} has not opted in to WhatsApp.")
            else:
                res.pass_check("whatsapp_consent")

        # Quiet hours check (22:00 to 08:00 IST)
        if is_quiet_hours_ist(send_at_dt):
            res.fail_check("quiet_hours", f"Send time {iso_ts(send_at_dt)} falls in quiet hours (22:00-08:00 IST).")
        else:
            res.pass_check("quiet_hours")

        # Frequency cap check (at most 1 push or WhatsApp per user per IST day)
        send_day = get_ist_day(send_at_dt)
        prior_actions = self.state_store.get_actions_for_user(uid)
        same_day_msgs = 0
        for pact in prior_actions:
            if pact.get("type") in ("push", "whatsapp"):
                p_send = parse_ts(pact.get("send_at") or pact.get("published_at"))
                if p_send and get_ist_day(p_send) == send_day:
                    same_day_msgs += 1

        if same_day_msgs >= 1:
            res.fail_check("frequency_cap", f"User {uid} already received/scheduled {same_day_msgs} messages on {send_day} IST.")
        else:
            res.pass_check("frequency_cap")

        # Copy & Template checks
        if act_type == "push":
            text = action.get("text") or {}
            title = text.get("title", "")
            body = text.get("body", "")
            if len(title) > 40:
                res.fail_check("push_title_len", f"Push title exceeds 40 chars ({len(title)}).")
            if len(body) > 120:
                res.fail_check("push_body_len", f"Push body exceeds 120 chars ({len(body)}).")
            # Forbidden phrases
            combined = (title + " " + body).lower()
            for phrase in FORBIDDEN_PUSH_PHRASES:
                if phrase in combined:
                    res.fail_check("push_forbidden_phrase", f"Push contains forbidden phrase: '{phrase}'.")
            # Deeplink format
            deeplink = action.get("deeplink", "")
            if not (deeplink.startswith("ott://title/") or deeplink == ""):
                res.fail_check("push_deeplink", f"Invalid deeplink format: {deeplink}")
            if "push_title_len" not in res.failed_checks and "push_forbidden_phrase" not in res.failed_checks:
                res.pass_check("push_copy_valid")

        elif act_type == "whatsapp":
            tmpl_id = action.get("template_id")
            if tmpl_id not in APPROVED_WA_TEMPLATES:
                res.fail_check("wa_template_approved", f"WhatsApp template '{tmpl_id}' is not in approved list.")
            else:
                res.pass_check("wa_template_approved")
                required_vars = APPROVED_WA_TEMPLATES[tmpl_id]
                vars_dict = action.get("variables") or {}
                missing = [v for v in required_vars if not vars_dict.get(v)]
                if missing:
                    res.fail_check("wa_variables_complete", f"Missing WhatsApp variables: {missing}")
                else:
                    res.pass_check("wa_variables_complete")
                # Pricing variable check
                if "plan_price" in vars_dict:
                    val = str(vars_dict["plan_price"])
                    valid_strs = ["₹1", "₹149", "₹399", "1", "149", "399"]
                    if not any(vs in val for vs in valid_strs):
                        res.fail_check("wa_pricing_invariant", f"WhatsApp mentioned unapproved price '{val}'.")
                    else:
                        res.pass_check("wa_pricing_invariant")
