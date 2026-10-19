"""SQLite-backed viewer state store with provenance, idempotency, and durable audit logs."""
import sqlite3
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from src.config import parse_ts, iso_ts, get_ist_day

class ViewerFact:
    def __init__(self, key: str, value: Any, source: str, observed_at: str):
        self.key = key
        self.value = value
        self.source = source
        self.observed_at = observed_at

    def freshness_seconds(self, current_time: datetime) -> float:
        obs = parse_ts(self.observed_at)
        if not obs:
            return 0.0
        return max(0.0, (current_time - obs).total_seconds())

    def to_dict(self, current_time: Optional[datetime] = None) -> Dict[str, Any]:
        d = {
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at,
        }
        if current_time:
            d["freshness_s"] = round(self.freshness_seconds(current_time), 1)
        return d

class StateStore:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

            # Events table: enforces global event_id uniqueness (deduplication)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    occurred_at TEXT,
                    received_at TEXT NOT NULL,
                    deliver_at TEXT,
                    session_id TEXT,
                    app_dialect TEXT,
                    payload_json TEXT NOT NULL
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, received_at)")

            # Facts table with provenance: (value, source, observed_at)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS viewer_facts (
                    user_id TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, fact_key)
                )
            """)

            # Active & historic goals per viewer (only one active at a time)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS viewer_goals (
                    goal_instance_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    status TEXT NOT NULL, -- OPEN, SUCCEEDED, FAILED, INCONCLUSIVE, CANCELLED
                    started_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    success_signal TEXT,
                    success_condition_json TEXT,
                    judged_at TEXT,
                    judgment_reason TEXT
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_user ON viewer_goals(user_id, status)")

            # Decisions audit table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    trigger_event_id TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_trigger ON decisions(trigger_event_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions(user_id)")

            # Actions table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    trigger_event_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    action_json TEXT NOT NULL
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_user ON actions(user_id)")

            # Lessons learned per viewer
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS viewer_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    lesson_text TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
            """)

    def is_event_seen(self, event_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,))
        return cur.fetchone() is not None

    def record_event(self, event: Dict[str, Any]) -> bool:
        """Records an event if not already present. Returns True if inserted, False if duplicate."""
        eid = event.get("event_id")
        if not eid or self.is_event_seen(eid):
            return False
        with self.conn:
            self.conn.execute("""
                INSERT INTO events (event_id, user_id, event_name, occurred_at, received_at, deliver_at, session_id, app_dialect, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                eid,
                event.get("user_id"),
                event.get("event"),
                event.get("occurred_at"),
                event.get("received_at"),
                event.get("deliver_at"),
                event.get("session_id"),
                event.get("app_dialect"),
                json.dumps(event, ensure_ascii=False)
            ))
        return True

    def record_events_batch(self, events: List[Dict[str, Any]]) -> int:
        """Inserts a batch of events within a single transaction for maximum I/O performance."""
        rows = []
        for e in events:
            eid = e.get("event_id")
            if eid:
                rows.append((
                    eid,
                    e.get("user_id"),
                    e.get("event"),
                    e.get("occurred_at"),
                    e.get("received_at"),
                    e.get("deliver_at"),
                    e.get("session_id"),
                    e.get("app_dialect"),
                    json.dumps(e, ensure_ascii=False)
                ))
        if rows:
            with self.conn:
                self.conn.executemany("""
                    INSERT OR IGNORE INTO events (event_id, user_id, event_name, occurred_at, received_at, deliver_at, session_id, app_dialect, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
        return len(rows)

    def get_events_for_user(self, user_id: str, up_to_ts: Optional[str] = None) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        if up_to_ts:
            cur.execute("""
                SELECT payload_json FROM events 
                WHERE user_id = ? AND received_at <= ?
                ORDER BY received_at ASC
            """, (user_id, up_to_ts))
        else:
            cur.execute("""
                SELECT payload_json FROM events 
                WHERE user_id = ? 
                ORDER BY received_at ASC
            """, (user_id,))
        return [json.loads(r["payload_json"]) for r in cur.fetchall()]

    def set_fact(self, user_id: str, key: str, value: Any, source: str, observed_at: str):
        with self.conn:
            self.conn.execute("""
                INSERT INTO viewer_facts (user_id, fact_key, fact_value_json, source, observed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, fact_key) DO UPDATE SET
                    fact_value_json=excluded.fact_value_json,
                    source=excluded.source,
                    observed_at=excluded.observed_at
            """, (user_id, key, json.dumps(value, ensure_ascii=False), source, observed_at))

    def get_fact(self, user_id: str, key: str) -> Optional[ViewerFact]:
        cur = self.conn.cursor()
        cur.execute("SELECT fact_value_json, source, observed_at FROM viewer_facts WHERE user_id = ? AND fact_key = ?", (user_id, key))
        row = cur.fetchone()
        if not row:
            return None
        return ViewerFact(key, json.loads(row["fact_value_json"]), row["source"], row["observed_at"])

    def get_all_facts(self, user_id: str) -> Dict[str, ViewerFact]:
        cur = self.conn.cursor()
        cur.execute("SELECT fact_key, fact_value_json, source, observed_at FROM viewer_facts WHERE user_id = ?", (user_id,))
        facts = {}
        for r in cur.fetchall():
            facts[r["fact_key"]] = ViewerFact(r["fact_key"], json.loads(r["fact_value_json"]), r["source"], r["observed_at"])
        return facts

    # Active goal management
    def get_active_goal(self, user_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT * FROM viewer_goals 
            WHERE user_id = ? AND status = 'OPEN'
            ORDER BY started_at DESC LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("success_condition_json"):
            d["success_condition"] = json.loads(d["success_condition_json"])
        return d

    def open_goal(self, user_id: str, goal_id: str, started_at: str, deadline_at: str,
                  success_signal: str, success_condition: Dict[str, Any], rule_version: str = "2026-09-30") -> str:
        # Cancel any previous OPEN goal to maintain strict invariant: exactly one active goal per viewer
        with self.conn:
            self.conn.execute("""
                UPDATE viewer_goals SET status = 'CANCELLED', judged_at = ?, judgment_reason = 'superceded_by_new_goal'
                WHERE user_id = ? AND status = 'OPEN'
            """, (started_at, user_id))
            
            goal_inst_id = f"G_INST_{user_id}_{goal_id}_{int(parse_ts(started_at).timestamp())}"
            self.conn.execute("""
                INSERT INTO viewer_goals (
                    goal_instance_id, user_id, goal_id, rule_version, status, started_at, deadline_at,
                    success_signal, success_condition_json
                ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
            """, (
                goal_inst_id, user_id, goal_id, rule_version, started_at, deadline_at,
                success_signal, json.dumps(success_condition, ensure_ascii=False)
            ))
        return goal_inst_id

    def update_open_goal_rules(self, goal_id: str, new_success_signal: str, new_condition: Dict[str, Any], new_version: str) -> int:
        """Applies goal rule corrections ONLY to currently OPEN goals of that goal_id."""
        with self.conn:
            cur = self.conn.execute("""
                UPDATE viewer_goals 
                SET success_signal = ?, success_condition_json = ?, rule_version = ?
                WHERE goal_id = ? AND status = 'OPEN'
            """, (new_success_signal, json.dumps(new_condition, ensure_ascii=False), new_version, goal_id))
            return cur.rowcount

    def cancel_pending_actions(self, user_id: str, cancelled_at: str, reason: str = "situation_changed") -> int:
        """Cancels scheduled actions (push/whatsapp) when a situation change makes them obsolete."""
        cur = self.conn.cursor()
        cur.execute("SELECT action_id, action_json FROM actions WHERE user_id = ? AND action_type IN ('push', 'whatsapp')", (user_id,))
        cancelled_count = 0
        with self.conn:
            for r in cur.fetchall():
                act = json.loads(r["action_json"])
                send_at = act.get("send_at")
                if send_at and send_at > cancelled_at:
                    act["status"] = "CANCELLED"
                    act["cancellation_reason"] = reason
                    self.conn.execute("UPDATE actions SET action_json = ? WHERE action_id = ?", (json.dumps(act), r["action_id"]))
                    cancelled_count += 1
        return cancelled_count

    def close_goal(self, goal_instance_id: str, status: str, judged_at: str, reason: str):
        with self.conn:
            self.conn.execute("""
                UPDATE viewer_goals 
                SET status = ?, judged_at = ?, judgment_reason = ?
                WHERE goal_instance_id = ?
            """, (status, judged_at, reason, goal_instance_id))

    # Decisions and Actions
    def record_decision(self, decision: Dict[str, Any]):
        with self.conn:
            self.conn.execute("""
                INSERT INTO decisions (decision_id, user_id, trigger_event_id, decision_json, decided_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                decision["decision_id"],
                decision["viewer_id"],
                decision["trigger"]["trigger_event_id"],
                json.dumps(decision, ensure_ascii=False),
                decision["trigger"]["received_at"],
                decision["status"]
            ))

    def get_decision_by_trigger(self, trigger_event_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT decision_json FROM decisions WHERE trigger_event_id = ?", (trigger_event_id,))
        row = cur.fetchone()
        if not row:
            return None
        return json.loads(row["decision_json"])

    def record_action(self, action: Dict[str, Any]):
        with self.conn:
            self.conn.execute("""
                INSERT INTO actions (action_id, decision_id, user_id, trigger_event_id, action_type, published_at, action_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                action["action_id"],
                action["decision_id"],
                action["user_id"],
                action["trigger_event_id"],
                action["type"],
                action["published_at"],
                json.dumps(action, ensure_ascii=False)
            ))

    def get_actions_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT action_json FROM actions WHERE user_id = ? ORDER BY published_at ASC", (user_id,))
        return [json.loads(r["action_json"]) for r in cur.fetchall()]

    def record_lesson(self, user_id: str, lesson_text: str, recorded_at: str):
        with self.conn:
            self.conn.execute("""
                INSERT INTO viewer_lessons (user_id, lesson_text, recorded_at)
                VALUES (?, ?, ?)
            """, (user_id, lesson_text, recorded_at))

    def get_lessons(self, user_id: str) -> List[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT lesson_text FROM viewer_lessons WHERE user_id = ? ORDER BY id ASC", (user_id,))
        return [r["lesson_text"] for r in cur.fetchall()]

    def export_viewer_state(self, user_id: str, as_of_time: datetime) -> Dict[str, Any]:
        """Durable snapshot of what the agent knows and decided for one viewer."""
        facts = {k: v.to_dict(as_of_time) for k, v in self.get_all_facts(user_id).items()}
        active_goal = self.get_active_goal(user_id)
        cur = self.conn.cursor()
        cur.execute("SELECT decision_json FROM decisions WHERE user_id = ? ORDER BY decided_at ASC", (user_id,))
        decisions = [json.loads(r["decision_json"]) for r in cur.fetchall()]
        actions = self.get_actions_for_user(user_id)
        lessons = self.get_lessons(user_id)

        return {
            "viewer_id": user_id,
            "exported_at": iso_ts(as_of_time),
            "facts": facts,
            "active_goal": active_goal,
            "decisions_count": len(decisions),
            "actions_count": len(actions),
            "recent_decision": decisions[-1] if decisions else None,
            "actions": actions,
            "lessons": lessons
        }
