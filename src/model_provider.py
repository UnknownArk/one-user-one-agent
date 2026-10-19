"""Isolated model provider interface with deterministic mock/replay implementation and fault injection."""
import json
import logging
import os
import time
from typing import Dict, List, Optional, Any, Tuple

from src.config import MAX_MODEL_CALLS, MAX_TOOL_CALLS

logger = logging.getLogger("stage.model_provider")

class ModelFaultException(Exception):
    pass

class ModelTimeoutException(ModelFaultException):
    pass

class ModelMalformedOutputException(ModelFaultException):
    pass

class ModelUnavailable503Exception(ModelFaultException):
    pass


class ModelProvider:
    """Base interface for model providers. Model is swappable and strictly bounded."""
    def rank_candidates(self, candidate_ids: List[str], viewer_context: Dict[str, Any], trigger_event_id: str) -> List[str]:
        raise NotImplementedError

    def query_claim_fault(self, trigger_event_id: str) -> Optional[Dict[str, Any]]:
        return None


class MockReplayModelProvider(ModelProvider):
    """Deterministic offline replay model provider reproducing faults from model_faults.jsonl."""
    def __init__(self, pack_dir: str):
        self.pack_dir = pack_dir
        self.faults: Dict[str, Dict[str, Any]] = {}
        self.is_service_503: bool = False
        self._load_faults()

    def _load_faults(self):
        faults_path = os.path.join(self.pack_dir, "replay", "model_faults.jsonl")
        if os.path.exists(faults_path):
            with open(faults_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        d = json.loads(line)
                        self.faults[d["trigger_event_id"]] = d

    def set_service_unavailable(self, unavailable: bool):
        self.is_service_503 = unavailable

    def query_claim_fault(self, trigger_event_id: str) -> Optional[Dict[str, Any]]:
        fault_entry = self.faults.get(trigger_event_id)
        if fault_entry and fault_entry.get("fault") == "unsupported_claim":
            logger.warning("MockModel: Injected unsupported_claim fault for trigger %s", trigger_event_id)
            return fault_entry.get("model_output")
        return None

    def rank_candidates(self, candidate_ids: List[str], viewer_context: Dict[str, Any], trigger_event_id: str) -> List[str]:
        """Ranks candidate title IDs filtered by rules. Never generates new unvalidated IDs."""
        if self.is_service_503:
            raise ModelUnavailable503Exception("HTTP 503: Model provider unavailable during control window")

        fault_entry = self.faults.get(trigger_event_id)
        if fault_entry:
            fault_type = fault_entry.get("fault")
            if fault_type == "timeout":
                logger.warning("MockModel: Injected timeout fault for trigger %s", trigger_event_id)
                raise ModelTimeoutException("Model call timed out after configured deadline")
            elif fault_type == "malformed_output":
                logger.warning("MockModel: Injected malformed_output fault for trigger %s", trigger_event_id)
                raw_out = fault_entry.get("model_output", "{malformed")
                try:
                    json.loads(raw_out)
                except Exception as e:
                    raise ModelMalformedOutputException(f"JSONDecodeError: {e}")

        # Deterministic ranking heuristic based on viewer's favorite genre and release date
        top_genre = viewer_context.get("top_genre")
        catalogue = viewer_context.get("catalogue")

        if not catalogue or not top_genre:
            return list(candidate_ids)

        def score(tid: str) -> Tuple[int, str]:
            t = catalogue.get(tid)
            if not t:
                return (0, "")
            genre_match = 1 if top_genre in t.get("genres", []) else 0
            release_dt = t.get("release_date") or ""
            return (genre_match, release_dt)

        return sorted(candidate_ids, key=score, reverse=True)
