"""The IBO partial-credit curve and the graders built on it."""
from __future__ import annotations

import pytest

from app.models.enums import QuestionType
from app.services.scoring import (
    default_curve,
    expected_guessing_score,
    grade_answer,
    grade_tf_block,
    resolve_curve,
)
from tests.conftest import make_question


def test_official_ibo_curve():
    """The published IBO Theory Part A scale for a four-statement block."""
    assert default_curve(4) == [0.0, 0.0, 0.2, 0.6, 1.0]


def test_curve_generalises_without_rewarding_half_marks():
    for n in range(1, 12):
        curve = default_curve(n)
        assert len(curve) == n + 1
        assert curve[n] == 1.0
        assert curve[0] == 0.0
        # Monotonically non-decreasing: more correct can never score less.
        assert all(b >= a for a, b in zip(curve, curve[1:]))


def test_question_curve_beats_exam_curve_beats_default():
    assert resolve_curve(4, {"4": [0, 0, 0.5, 0.5, 1]}, {"4": [0, 0, 0, 0, 1]}) == [
        0, 0, 0.5, 0.5, 1
    ]
    assert resolve_curve(4, None, {"4": [0, 0, 0, 0, 1]}) == [0, 0, 0, 0, 1]
    assert resolve_curve(4, None, None) == [0.0, 0.0, 0.2, 0.6, 1.0]
    # A curve of the wrong length is ignored rather than silently mis-scoring.
    assert resolve_curve(4, {"4": [0, 1]}, None) == [0.0, 0.0, 0.2, 0.6, 1.0]


@pytest.mark.parametrize(
    "given,expected_correct,expected_points",
    [
        ([True, False, True, False], 4, 1.0),
        ([True, False, True, True], 3, 0.6),
        ([True, False, False, True], 2, 0.2),
        ([True, True, False, True], 1, 0.0),
        ([False, True, False, True], 0, 0.0),
    ],
)
def test_tf_block_scores_on_the_curve(db, given, expected_correct, expected_points):
    question = make_question(db, truths=(True, False, True, False))
    response = {str(s.id): given[i] for i, s in enumerate(question.statements)}
    result = grade_tf_block(question.statements, response, max_points=1.0)
    assert result.correct_count == expected_correct
    assert result.awarded == pytest.approx(expected_points)


def test_blank_statement_counts_as_wrong(db):
    """A blank cell on the real answer sheet earns nothing; so does a null here."""
    question = make_question(db, truths=(True, True, True, True))
    statements = question.statements
    response = {str(statements[0].id): True, str(statements[1].id): None}
    result = grade_tf_block(statements, response, max_points=1.0)
    assert result.correct_count == 1
    assert result.awarded == 0.0


def test_per_statement_correctness_is_recorded(db):
    """Statement-level correctness is what the analytics are built on."""
    question = make_question(db, truths=(True, False, True, False))
    statements = question.statements
    response = {
        str(statements[0].id): True,
        str(statements[1].id): True,
        str(statements[2].id): True,
        str(statements[3].id): False,
    }
    result = grade_tf_block(statements, response, max_points=1.0)
    assert result.per_statement == {
        str(statements[0].id): True,
        str(statements[1].id): False,
        str(statements[2].id): True,
        str(statements[3].id): True,
    }


def test_points_scale_with_question_weight(db):
    question = make_question(db, truths=(True, False, True, False))
    response = {str(s.id): s.is_true for s in question.statements}
    result = grade_tf_block(question.statements, response, max_points=2.5)
    assert result.awarded == pytest.approx(2.5)


def test_guessing_expectation_matches_hand_calculation():
    """P(k correct) is binomial; E = sum C(4,k) * curve[k] / 16."""
    # (6*0.2 + 4*0.6 + 1*1.0) / 16
    assert expected_guessing_score(4) == pytest.approx(4.6 / 16)


def test_mcq_single_is_all_or_nothing(db):
    question = make_question(db, truths=(True, False, False, False))
    question.question_type = QuestionType.MCQ_SINGLE
    statements = question.statements

    right = grade_answer(question, {str(statements[0].id): True}, 1.0)
    assert right.awarded == 1.0

    # Selecting the correct option *and* a wrong one is not a correct answer.
    hedged = grade_answer(
        question, {str(statements[0].id): True, str(statements[1].id): True}, 1.0
    )
    assert hedged.awarded == 0.0


def test_mcq_multi_subtracts_for_wrong_selections(db):
    question = make_question(db, truths=(True, True, False, False))
    question.question_type = QuestionType.MCQ_MULTI
    statements = question.statements

    both = grade_answer(
        question, {str(statements[0].id): True, str(statements[1].id): True}, 1.0
    )
    assert both.awarded == pytest.approx(1.0)

    # One right, one wrong nets to zero rather than half marks.
    mixed = grade_answer(
        question, {str(statements[0].id): True, str(statements[2].id): True}, 1.0
    )
    assert mixed.awarded == 0.0


def test_numeric_respects_tolerance(db):
    question = make_question(db, truths=())
    question.question_type = QuestionType.NUMERIC
    question.answer_spec = {"value": 12.5, "tolerance": 0.2}
    assert grade_answer(question, {"value": 12.6}, 1.0).awarded == 1.0
    assert grade_answer(question, {"value": 13.0}, 1.0).awarded == 0.0
    assert grade_answer(question, {"value": "not a number"}, 1.0).awarded == 0.0


def test_short_text_is_case_insensitive_by_default(db):
    question = make_question(db, truths=())
    question.question_type = QuestionType.SHORT_TEXT
    question.answer_spec = {"accept": ["Mitochondrion", "митохондрия"]}
    assert grade_answer(question, {"text": "  mitochondrion "}, 1.0).awarded == 1.0
    assert grade_answer(question, {"text": "МИТОХОНДРИЯ"}, 1.0).awarded == 1.0
    assert grade_answer(question, {"text": "chloroplast"}, 1.0).awarded == 0.0


def test_open_response_defers_to_a_human(db):
    question = make_question(db, truths=())
    question.question_type = QuestionType.OPEN_RESPONSE
    result = grade_answer(question, {"text": "an essay"}, 5.0)
    assert result.needs_manual_grading is True
    assert result.awarded == 0.0
