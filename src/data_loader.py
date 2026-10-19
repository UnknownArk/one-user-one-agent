"""Data loader and fact resolver with source precedence, deduplication, and injection protection."""
import csv
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Any, Set, Tuple

from src.config import parse_ts, iso_ts, VALID_PLANS

logger = logging.getLogger("stage.data_loader")

# Pattern-based prompt injection detection for catalogue descriptions (generic, no hardcoded IDs)
INJECTION_PATTERNS = [
    re.compile(r"system\s+(?:note|instruction|prompt)", re.IGNORECASE),
    re.compile(r"assistant\s+instruction", re.IGNORECASE),
    re.compile(r"ignore\s+(?:your\s+)?(?:previous|all)\s+rules", re.IGNORECASE),
    re.compile(r"message\s+every\s+user", re.IGNORECASE),
    re.compile(r"mark\s+every\s+user", re.IGNORECASE),
    re.compile(r"pre-approved\s*:", re.IGNORECASE),
]

def sanitize_catalogue_text(text: str, title_id: str) -> str:
    """Detects and neutralizes prompt injections inside catalogue descriptions."""
    sanitized = text
    detected = False
    for pattern in INJECTION_PATTERNS:
        match = pattern.search(sanitized)
        if match:
            detected = True
            idx = match.start()
            sanitized = sanitized[:idx].rstrip() + " [REDACTED_INSTRUCTION]"
            break
    if detected:
        logger.warning("Prompt injection attempt detected in title %s! Neutralizing content.", title_id)
    return sanitized


class CatalogueManager:
    def __init__(self):
        self.titles: Dict[str, Dict[str, Any]] = {}
        self.titles_by_dialect: Dict[str, List[str]] = defaultdict(list)
        self.slug_to_titles: Dict[str, List[str]] = defaultdict(list)
        self.withdrawn_titles: Dict[str, datetime] = {}
        self.detected_injections: List[Dict[str, str]] = []

    def load_from_pack(self, pack_dir: str):
        path = os.path.join(pack_dir, "data", "catalogue.csv")
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                tid = r["title_id"]
                raw_desc = r.get("description", "")
                
                # Check for prompt injection
                has_injection = any(p.search(raw_desc) for p in INJECTION_PATTERNS)
                if has_injection:
                    self.detected_injections.append({"title_id": tid, "raw_description": raw_desc})
                    desc = sanitize_catalogue_text(raw_desc, tid)
                else:
                    desc = raw_desc

                genres = [g.strip() for g in r["genres"].split("|") if g.strip()]
                title_data = {
                    "title_id": tid,
                    "content_slug": r["content_slug"],
                    "title": r["title"],
                    "dialect": r["dialect"],
                    "language": r["language"],
                    "content_type": r["content_type"],
                    "genres": genres,
                    "release_date": r.get("release_date"),
                    "available_from": parse_ts(r["available_from"]),
                    "available_until": parse_ts(r["available_until"]) if r.get("available_until") else None,
                    "is_premium": r["is_premium"].lower() == "true",
                    "description": desc,
                }
                self.titles[tid] = title_data
                self.titles_by_dialect[r["dialect"]].append(tid)
                self.slug_to_titles[r["content_slug"]].append(tid)

    def withdraw_title(self, title_id: str, at: datetime, reason: str = ""):
        self.withdrawn_titles[title_id] = at
        if title_id in self.titles:
            current_until = self.titles[title_id]["available_until"]
            if current_until is None or at < current_until:
                self.titles[title_id]["available_until"] = at
        logger.info("Title %s withdrawn effective %s: %s", title_id, iso_ts(at), reason)

    def is_available(self, title_id: str, at: datetime) -> bool:
        t = self.titles.get(title_id)
        if not t:
            return False
        if t["available_from"] and at < t["available_from"]:
            return False
        if t["available_until"] and at >= t["available_until"]:
            return False
        if title_id in self.withdrawn_titles and at >= self.withdrawn_titles[title_id]:
            return False
        return True


class EpisodeManager:
    def __init__(self):
        # slug -> list of (season, episode, episode_id, minutes)
        self.episodes_by_slug: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.episode_to_slug: Dict[str, str] = {}

    def load_from_pack(self, pack_dir: str):
        path = os.path.join(pack_dir, "data", "episodes.csv")
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                slug = r["content_slug"]
                ep_id = r["episode_id"]
                item = {
                    "episode_id": ep_id,
                    "content_slug": slug,
                    "season": int(r["season"]),
                    "episode": int(r["episode"]),
                    "minutes": int(r.get("minutes", 0)) if r.get("minutes") else 0,
                }
                self.episodes_by_slug[slug].append(item)
                self.episode_to_slug[ep_id] = slug
        
        # Sort episodes chronologically by season and episode
        for slug in self.episodes_by_slug:
            self.episodes_by_slug[slug].sort(key=lambda x: (x["season"], x["episode"]))

    def get_episodes(self, content_slug: str) -> List[Dict[str, Any]]:
        return self.episodes_by_slug.get(content_slug, [])

    def get_next_episode(self, content_slug: str, last_completed_ep_id: Optional[str]) -> Optional[Dict[str, Any]]:
        eps = self.episodes_by_slug.get(content_slug, [])
        if not eps:
            return None
        if not last_completed_ep_id:
            return eps[0]
        for i, ep in enumerate(eps):
            if ep["episode_id"] == last_completed_ep_id:
                if i + 1 < len(eps):
                    return eps[i + 1]
                return None  # All episodes watched!
        return eps[0]


class PackDataLoader:
    def __init__(self, pack_dir: str):
        self.pack_dir = pack_dir
        self.catalogue = CatalogueManager()
        self.episodes = EpisodeManager()
        self.arms: Dict[str, str] = {}
        self.raw_profiles: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.goals_config: Dict[str, Any] = {}
        self.interventions_config: Dict[str, Any] = {}
        self.pricing_config: Dict[str, Any] = {}

    def load_all(self):
        self.catalogue.load_from_pack(self.pack_dir)
        self.episodes.load_from_pack(self.pack_dir)

        # Arms
        arms_path = os.path.join(self.pack_dir, "replay", "arms.csv")
        with open(arms_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.arms[r["user_id"]] = r["arm"]

        # Profiles snapshot (handling duplicates)
        profiles_path = os.path.join(self.pack_dir, "data", "profiles_snapshot.csv")
        with open(profiles_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.raw_profiles[r["user_id"]].append(r)

        # Configs
        with open(os.path.join(self.pack_dir, "config", "goals.json"), encoding="utf-8") as f:
            self.goals_config = json.load(f)

        with open(os.path.join(self.pack_dir, "config", "interventions.json"), encoding="utf-8") as f:
            self.interventions_config = json.load(f)

        with open(os.path.join(self.pack_dir, "config", "pricing.json"), encoding="utf-8") as f:
            self.pricing_config = json.load(f)


def resolve_viewer_profile(user_id: str, candidate_profiles: List[Dict[str, Any]], user_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolves viewer profile facts with explicit precedence rules.
    
    Precedence rules:
    - If user has no profile row, facts are synthesized purely from events.
    - If user has duplicate profile rows: cross-reference against event stream.
      e.g. if events contain purchase_completed/subscription_lapsed, subscription_status
      from events takes strict precedence over outdated profile snapshot rows.
    - Newer events always beat the snapshot.
    """
    # Event-derived ground truths
    has_purchase = any(e["event"] == "purchase_completed" for e in user_events)
    has_renewal = any(e["event"] == "subscription_renewed" for e in user_events)
    has_lapsed = any(e["event"] == "subscription_lapsed" for e in user_events)
    latest_event_time = user_events[-1]["received_at"] if user_events else "1970-01-01T00:00:00.000Z"
    
    signup_events = [e for e in user_events if e["event"] == "signup_completed"]
    event_signup_at = signup_events[0]["received_at"] if signup_events else None

    # Opt-outs in events
    opt_outs = {e.get("properties", {}).get("channel") for e in user_events if e["event"] == "notification_opt_out"}

    if not candidate_profiles:
        # User has no row in profiles_snapshot.csv (e.g. brand-new user)
        status = "never"
        if has_renewal or (has_purchase and not has_lapsed):
            status = "active"
        elif has_lapsed:
            status = "lapsed"

        return {
            "user_id": user_id,
            "signup_at": event_signup_at or (user_events[0]["received_at"] if user_events else latest_event_time),
            "subscription_status": status,
            "status_source": "events_synthesis",
            "preferred_dialect": user_events[0].get("app_dialect", "d1") if user_events else "d1",
            "preferred_language": user_events[0].get("app_language", "hin") if user_events else "hin",
            "notifications_opt_in": "push" not in opt_outs,
            "whatsapp_opt_in": "whatsapp" not in opt_outs,
            "platform": user_events[0].get("platform", "android") if user_events else "android",
            "profile_conflict": False,
        }

    # If duplicate profile rows exist (the 30 duplicate users in snapshot)
    if len(candidate_profiles) > 1:
        # Cross-reference with events: if events show purchase history, status should reflect it
        best_p = None
        for p in candidate_profiles:
            p_status = p["subscription_status"]
            # If events show subscription lapse, a profile stating 'lapsed' or 'active' matches history better than 'never'
            if (has_purchase or has_lapsed) and p_status in ("active", "lapsed", "trial_active"):
                best_p = p
                break
        if not best_p:
            # Fallback to row with newest updated_at
            best_p = max(candidate_profiles, key=lambda x: x["updated_at"])
    else:
        best_p = candidate_profiles[0]

    # Precedence: fresh purchase/lapsed events override profile status
    status = best_p["subscription_status"]
    status_source = "profile_snapshot"
    if has_lapsed:
        # Check if latest lapse is after any purchase
        lapse_time = max(e["received_at"] for e in user_events if e["event"] == "subscription_lapsed")
        purchase_times = [e["received_at"] for e in user_events if e["event"] == "purchase_completed"]
        latest_purchase = max(purchase_times) if purchase_times else "1970-01-01T00:00:00.000Z"
        if lapse_time >= latest_purchase:
            status = "lapsed"
            status_source = "events_subscription_lapsed"
        else:
            status = "active"
            status_source = "events_purchase_completed"
    elif has_purchase:
        # Check if trial or paid
        last_purch = [e for e in user_events if e["event"] == "purchase_completed"][-1]
        is_trial = last_purch.get("properties", {}).get("is_trial", False)
        status = "trial_active" if is_trial else "active"
        status_source = "events_purchase_completed"

    push_opt_in = (best_p.get("notifications_opt_in") == "true") and ("push" not in opt_outs)
    wa_opt_in = (best_p.get("whatsapp_opt_in") == "true") and ("whatsapp" not in opt_outs)

    return {
        "user_id": user_id,
        "signup_at": event_signup_at or best_p.get("signup_at") or best_p.get("snapshot_at"),
        "subscription_status": status,
        "status_source": status_source,
        "preferred_dialect": best_p.get("preferred_dialect", "d1"),
        "preferred_language": best_p.get("preferred_language", "hin"),
        "notifications_opt_in": push_opt_in,
        "whatsapp_opt_in": wa_opt_in,
        "platform": best_p.get("platform", "android"),
        "profile_conflict": len(candidate_profiles) > 1,
    }
