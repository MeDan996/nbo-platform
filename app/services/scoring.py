"""Grading.

The house default is the IBO Theory Part A curve. A question with four
true/false statements is worth 1.0 point, awarded on how many of the four the
participant identified correctly:

    correct   0     1     2     3     4
    points    0.0   0.0   0.2   0.6   1.0

The curve is steep on purpose. Answering a four-statement block at random has an
expected value of 0.2875 points against a 0.5 expected hit rate, so guessing is
penalised relative to a naive "one mark per correct statement" scheme, while a
participant who genuinely knows three of four still earns meaningful credit.
``expected_guessing_score`` below computes this for any curve, and the author UI
surfaces it as a warning when a custom curve makes guessing too profitable.

Curves are stored as ``{"<statement_count>": [p0, p1, ..., pN]}`` and resolved
question-first, then exam, then this module's defaults.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.models.content import Question, Statement
from app.models.enums import QuestionType

# Official IBO curve for the standard four-statement block, plus sensible
# extensions for blocks of other sizes that keep the same shape: nothing below
# half correct, a steep climb to full marks.
DEFAULT_CURVES: dict[int, list[float]] = {
    1: [0.0, 1.0],
    2: [0.0, 0.0, 1.0],
    3: [0.0, 0.0, 0.4, 1.0],
    4: [0.0, 0.0, 0.2, 0.6, 1.0],
    5: [0.0, 0.0, 0.0, 0.2, 0.6, 1.0],
    6: [0.0, 0.0, 0.0, 0.15, 0.35, 0.65, 1.0],
}


def default_curve(n: int) -> list[float]:
    """Fraction of full marks for 0..n correct statements."""
    if n in DEFAULT_CURVES:
        return DEFAULT_CURVES[n]
    if n <= 0:
        return [0.0]
    # Generalisation: no credit below half, then a convex ramp to 1.0.
    floor = (n + 1) // 2
    curve = [0.0] * (n + 1)
    span = n - floor
    for correct in range(floor, n + 1):
        if span == 0:
            curve[correct] = 1.0
        else:
            curve[correct] = round(((correct - floor) / span) ** 2, 4)
    curve[n] = 1.0
    return curve


def resolve_curve(
    statement_count: int,
    question_curve: dict | None = None,
    exam_curve: dict | None = None,
) -> list[float]:
    """Question-level curve wins, then the exam's, then the default."""
    key = str(statement_count)
    for source in (question_curve, exam_curve):
        if source and key in source:
            curve = [float(x) for x in source[key]]
            if len(curve) == statement_count + 1:
                return curve
    return default_curve(statement_count)


def _norm_bool(value: Any) -> bool | None:
    """Accept the several shapes a true/false answer arrives in."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"t", "true", "1", "yes", "y", "да", "ооба"}:
        return True
    if text in {"f", "false", "0", "no", "n", "нет", "жок"}:
        return False
    return None


class GradeResult:
    __slots__ = (
        "awarded",
        "max_points",
        "correct_count",
        "statement_count",
        "per_statement",
        "is_fully_correct",
        "needs_manual_grading",
    )

    def __init__(
        self,
        awarded: float,
        max_points: float,
        correct_count: int,
        statement_count: int,
        per_statement: dict[str, bool],
        needs_manual_grading: bool = False,
    ) -> None:
        self.awarded = awarded
        self.max_points = max_points
        self.correct_count = correct_count
        self.statement_count = statement_count
        self.per_statement = per_statement
        self.is_fully_correct = statement_count > 0 and correct_count == statement_count
        self.needs_manual_grading = needs_manual_grading

    @property
    def fraction(self) -> float:
        return self.awarded / self.max_points if self.max_points else 0.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<GradeResult {self.awarded}/{self.max_points} "
            f"({self.correct_count}/{self.statement_count} statements)>"
        )


def grade_tf_block(
    statements: Iterable[Statement],
    response: dict,
    max_points: float,
    question_curve: dict | None = None,
    exam_curve: dict | None = None,
) -> GradeResult:
    """Grade an IBO-style true/false block.

    An unanswered statement counts as wrong, matching the real exam where a
    blank cell on the answer sheet earns nothing.
    """
    statements = list(statements)
    n = len(statements)
    per_statement: dict[str, bool] = {}
    correct = 0

    for st in statements:
        given = _norm_bool(response.get(str(st.id)))
        hit = given is not None and given == st.is_true
        per_statement[str(st.id)] = hit
        if hit:
            correct += 1

    curve = resolve_curve(n, question_curve, exam_curve)
    awarded = round(curve[correct] * max_points, 4)
    return GradeResult(awarded, max_points, correct, n, per_statement)


def grade_mcq(
    statements: Iterable[Statement],
    response: dict,
    max_points: float,
    single: bool,
) -> GradeResult:
    """Single-answer MCQ is all-or-nothing. Multi-select gives partial credit
    but subtracts for each wrong selection, floored at zero."""
    statements = list(statements)
    correct_ids = {str(s.id) for s in statements if s.is_true}
    chosen = {str(k) for k, v in response.items() if _norm_bool(v)}
    per_statement = {str(s.id): ((str(s.id) in chosen) == s.is_true) for s in statements}

    if single:
        hit = len(chosen) == 1 and chosen == correct_ids
        awarded = max_points if hit else 0.0
        return GradeResult(
            round(awarded, 4), max_points, 1 if hit else 0, 1, per_statement
        )

    if not correct_ids:
        return GradeResult(0.0, max_points, 0, len(statements), per_statement)

    hits = len(chosen & correct_ids)
    misses = len(chosen - correct_ids)
    fraction = max(0.0, (hits - misses) / len(correct_ids))
    return GradeResult(
        round(fraction * max_points, 4),
        max_points,
        hits,
        len(correct_ids),
        per_statement,
    )


def grade_numeric(response: dict, spec: dict | None, max_points: float) -> GradeResult:
    spec = spec or {}
    target = spec.get("value")
    tolerance = float(spec.get("tolerance", 0) or 0)
    raw = response.get("value")
    try:
        given = float(raw)
    except (TypeError, ValueError):
        return GradeResult(0.0, max_points, 0, 1, {})
    if target is None:
        return GradeResult(0.0, max_points, 0, 1, {}, needs_manual_grading=True)
    hit = abs(given - float(target)) <= tolerance
    return GradeResult(max_points if hit else 0.0, max_points, int(hit), 1, {})


def grade_short_text(response: dict, spec: dict | None, max_points: float) -> GradeResult:
    spec = spec or {}
    accepted = [str(a) for a in spec.get("accept", [])]
    given = str(response.get("text") or "").strip()
    if not accepted:
        return GradeResult(0.0, max_points, 0, 1, {}, needs_manual_grading=True)
    if not spec.get("case_sensitive"):
        given_cmp = given.casefold()
        accepted_cmp = [a.casefold() for a in accepted]
    else:
        given_cmp, accepted_cmp = given, accepted
    hit = given_cmp in accepted_cmp
    return GradeResult(max_points if hit else 0.0, max_points, int(hit), 1, {})


def grade_answer(
    question: Question,
    response: dict | None,
    max_points: float,
    exam_curve: dict | None = None,
) -> GradeResult:
    """Dispatch to the grader for this question's type."""
    response = response or {}
    qtype = QuestionType(question.question_type)

    if qtype is QuestionType.TF_BLOCK:
        return grade_tf_block(
            question.statements, response, max_points, question.scoring_curve, exam_curve
        )
    if qtype in (QuestionType.MCQ_SINGLE, QuestionType.MCQ_MULTI):
        return grade_mcq(
            question.statements, response, max_points, single=qtype is QuestionType.MCQ_SINGLE
        )
    if qtype is QuestionType.NUMERIC:
        return grade_numeric(response, question.answer_spec, max_points)
    if qtype is QuestionType.SHORT_TEXT:
        return grade_short_text(response, question.answer_spec, max_points)
    # OPEN_RESPONSE: a human awards the marks.
    return GradeResult(0.0, max_points, 0, 1, {}, needs_manual_grading=True)


def expected_guessing_score(statement_count: int, curve: list[float] | None = None) -> float:
    """Expected fraction of marks from answering a block at random.

    Useful in the author UI: it warns when a curve makes guessing profitable.
    """
    from math import comb

    n = statement_count
    curve = curve or default_curve(n)
    total = 2**n
    return round(
        sum(comb(n, k) * curve[k] for k in range(n + 1)) / total,
        4,
    )
