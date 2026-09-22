"""Enumerations shared across the data model."""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    PARTICIPANT = "participant"
    AUTHOR = "author"
    REVIEWER = "reviewer"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {"participant": 0, "author": 1, "reviewer": 2, "admin": 3}[self.value]


class QuestionType(StrEnum):
    """`TF_BLOCK` is the IBO Theory Part A native format: a stem plus N
    true/false statements scored on a partial-credit curve."""

    TF_BLOCK = "tf_block"
    MCQ_SINGLE = "mcq_single"
    MCQ_MULTI = "mcq_multi"
    NUMERIC = "numeric"
    SHORT_TEXT = "short_text"
    OPEN_RESPONSE = "open_response"  # graded by a human (IBO Part B style)


class QuestionStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    RETIRED = "retired"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    COMMENT = "comment"


class Difficulty(StrEnum):
    INTRO = "intro"
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    OLYMPIAD = "olympiad"

    @property
    def weight(self) -> float:
        return {"intro": 0.6, "easy": 0.8, "medium": 1.0, "hard": 1.25, "olympiad": 1.5}[
            self.value
        ]


class ExamMode(StrEnum):
    PRACTICE = "practice"  # unranked, instant feedback, retakeable
    MOCK = "mock"  # ranked lightly, full timing
    OFFICIAL = "official"  # ranked, one attempt, full anti-cheat


class ExamStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    OPEN = "open"
    CLOSED = "closed"
    ARCHIVED = "archived"


class AttemptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    EXPIRED = "expired"  # ran out of time, auto-submitted
    GRADED = "graded"
    VOIDED = "voided"  # invalidated by an admin (confirmed cheating)


class FlagSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def score(self) -> int:
        return {"info": 0, "low": 10, "medium": 30, "high": 60, "critical": 100}[self.value]


class FlagStatus(StrEnum):
    OPEN = "open"
    REVIEWING = "reviewing"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class EventKind(StrEnum):
    """Client-side proctoring telemetry recorded during an attempt."""

    ATTEMPT_START = "attempt_start"
    ATTEMPT_RESUME = "attempt_resume"
    ANSWER_CHANGE = "answer_change"
    WINDOW_BLUR = "window_blur"
    WINDOW_FOCUS = "window_focus"
    FULLSCREEN_EXIT = "fullscreen_exit"
    TAB_HIDDEN = "tab_hidden"
    COPY = "copy"
    PASTE = "paste"
    CONTEXT_MENU = "context_menu"
    RESIZE = "resize"
    NETWORK_DROP = "network_drop"
    IP_CHANGE = "ip_change"
    SUBMIT = "submit"
