"""Bounded process-local handles shared by MCP and HTTP interfaces."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any, Optional

from fix_generation import FixCandidate
from review_schema import ReviewResponse


@dataclass(frozen=True)
class ReviewHandle:
    code: str
    response: ReviewResponse


class LifecycleState:
    """Small bounded in-memory state; deliberately not shared across processes."""

    def __init__(self, limit: int = 32) -> None:
        self.limit = limit
        # Legacy MCP callers/tests may store the prior tuple shape directly.
        self.reviews: OrderedDict[str, Any] = OrderedDict()
        self.candidates: OrderedDict[str, FixCandidate] = OrderedDict()
        self._lock = RLock()

    def put_review(self, review_id: str, code: str, response: ReviewResponse) -> None:
        with self._lock:
            self.reviews[review_id] = ReviewHandle(code, response)
            self.reviews.move_to_end(review_id)
            self._trim(self.reviews)

    def get_review(self, review_id: str) -> Optional[ReviewHandle]:
        if not isinstance(review_id, str):
            return None
        with self._lock:
            value = self.reviews.get(review_id)
            if value is not None:
                self.reviews.move_to_end(review_id)
            if isinstance(value, ReviewHandle):
                return value
            if isinstance(value, tuple) and len(value) == 2:
                return ReviewHandle(value[0], value[1])
            return None

    def put_candidate(self, candidate: FixCandidate) -> None:
        with self._lock:
            self.candidates[candidate.candidate_id] = candidate
            self.candidates.move_to_end(candidate.candidate_id)
            self._trim(self.candidates)

    def get_candidate(self, candidate_id: str) -> Optional[FixCandidate]:
        if not isinstance(candidate_id, str):
            return None
        with self._lock:
            value = self.candidates.get(candidate_id)
            if value is not None:
                self.candidates.move_to_end(candidate_id)
            return value

    def _trim(self, cache: OrderedDict) -> None:
        while len(cache) > self.limit:
            cache.popitem(last=False)


PROCESS_STATE = LifecycleState(limit=32)
