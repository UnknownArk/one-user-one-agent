"""Configuration, schemas, and invariants for One User, One Agent."""
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

# Timezones
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Operational invariants
VALID_PLANS = {
    "P_TRIAL": 1,
    "P_MONTH": 149,
    "P_QUARTER": 399,
}

FORBIDDEN_PUSH_PHRASES = [
    "last chance",
    "hurry",
    "only today",
    "free forever",
    "guaranteed",
]

# Timeouts & budgets
STALE_TRIGGER_THRESHOLD_SECONDS = 600  # 10 minutes (matching simulator threshold)
MAX_DECISION_LATENCY_MS = 60000        # 60s max per brief
MAX_MODEL_CALLS = 8
MAX_TOOL_CALLS = 20

# Home page layout constraints
MAX_HOME_ROWS = 6
MIN_ROW_TITLES = 3
MAX_ROW_TITLES = 10
HOME_PAGE_TTL_MINUTES = 120

# Approved WhatsApp templates (from config/interventions.json)
APPROVED_WA_TEMPLATES = {
    "wa_new_episode_v2": ["title_name", "episode_number"],
    "wa_recommend_title_v1": ["title_name", "dialect_name"],
    "wa_trial_ending_v1": ["plan_price"],
}

# Date and time helpers
def parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    cleaned = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt

def iso_ts(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def get_ist_day(dt: datetime) -> date:
    return dt.astimezone(IST).date()

def is_quiet_hours_ist(dt: datetime) -> bool:
    """True if local IST time is in 22:00 to 08:00."""
    hour = dt.astimezone(IST).hour
    return hour >= 22 or hour < 8

def next_morning_ist(dt: datetime) -> datetime:
    """Returns 08:05 IST on the next active window if in quiet hours."""
    ist_dt = dt.astimezone(IST)
    if ist_dt.hour >= 22:
        next_day = ist_dt.date() + timedelta(days=1)
        res = datetime(next_day.year, next_day.month, next_day.day, 8, 5, 0, tzinfo=IST)
    elif ist_dt.hour < 8:
        res = datetime(ist_dt.year, ist_dt.month, ist_dt.day, 8, 5, 0, tzinfo=IST)
    else:
        res = ist_dt
    return res.astimezone(UTC)

# Decision schemas
class TriggerInfo(BaseModel):
    event: str
    received_at: str
    event_age_s: float
    trigger_event_id: str

class DialectInfo(BaseModel):
    used: str
    source: str
    conflicts: List[str] = Field(default_factory=list)

class GoalDecisionInfo(BaseModel):
    id: str
    deadline: str
    signal: Optional[str] = None
    rule_version: str = "2026-09-30"

class ChecksInfo(BaseModel):
    passed: List[str] = Field(default_factory=list)
    failed: List[str] = Field(default_factory=list)

class DecisionRecord(BaseModel):
    decision_id: str
    viewer_id: str
    trigger: Dict[str, Any]
    goal: Optional[Dict[str, Any]] = None
    action: str  # HOME_PAGE, PUSH, WHATSAPP, IN_APP_MESSAGE, NO_ACTION
    dialect: Dict[str, Any]
    content: List[str] = Field(default_factory=list)
    confidence: float
    confidence_reason: str
    evidence: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    checks: Dict[str, List[str]] = Field(default_factory=lambda: {"passed": [], "failed": []})
    decided_in_ms: int
    status: str  # PUBLISHED, HOLDOUT_SUPPRESSED, REJECTED, ABSTAINED
    no_action_reason: Optional[str] = None
    model_calls: int = 0
    tool_calls: int = 0
    fallback_used: bool = False

class ActionRecord(BaseModel):
    action_id: str
    decision_id: str
    user_id: str
    trigger_event_id: str
    type: str  # home_page, push, whatsapp, in_app_message
    published_at: str
    dialect: Optional[str] = None
    hero_title_id: Optional[str] = None
    rows: Optional[List[Dict[str, Any]]] = None
    send_at: Optional[str] = None
    title_id: Optional[str] = None
    text: Optional[Dict[str, str]] = None
    deeplink: Optional[str] = None
    template_id: Optional[str] = None
    variables: Optional[Dict[str, str]] = None
