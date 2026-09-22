"""Contest rating behaviour, and the escape-first Markdown renderer."""
from __future__ import annotations

import pytest

from app.services.markup import plain, render
from app.services.rating import (
    Competitor,
    assign_ranks,
    compute_ratings,
    expected_score_fraction,
    performance_rating,
    tier_for,
)


# --------------------------------------------------------------------------- #
# Rating
# --------------------------------------------------------------------------- #
def test_ties_share_the_best_rank():
    field = [
        Competitor(1, 1200, score=40.0),
        Competitor(2, 1200, score=35.0),
        Competitor(3, 1200, score=35.0),
        Competitor(4, 1200, score=30.0),
    ]
    assign_ranks(field)
    assert [c.rank for c in field] == [1, 2, 2, 4]


def test_winning_gains_and_losing_loses():
    field = [Competitor(i, 1500, score=100 - i * 10) for i in range(1, 6)]
    compute_ratings(field)
    assert field[0].delta > 0
    assert field[-1].delta < 0


def test_the_pool_does_not_inflate():
    """Deltas must sum to roughly zero or below, or ratings drift upward forever."""
    field = [Competitor(i, 1200 + i * 40, score=float(i)) for i in range(1, 25)]
    compute_ratings(field)
    assert sum(c.delta for c in field) <= 0


def test_beating_expectations_is_what_earns_rating():
    """A weak competitor who finishes first gains far more than a strong one who does."""
    weak_wins = [
        Competitor(1, 900, score=100.0),
        *[Competitor(i, 2000, score=float(100 - i)) for i in range(2, 8)],
    ]
    compute_ratings(weak_wins)
    underdog_gain = next(c.delta for c in weak_wins if c.user_id == 1)

    favourite_wins = [
        Competitor(1, 2000, score=100.0),
        *[Competitor(i, 2000, score=float(100 - i)) for i in range(2, 8)],
    ]
    compute_ratings(favourite_wins)
    favourite_gain = next(c.delta for c in favourite_wins if c.user_id == 1)

    assert underdog_gain > favourite_gain


def test_single_competitor_is_left_untouched():
    """One person is not a contest; their rating must not move."""
    solo = [Competitor(1, 1450, score=10.0)]
    compute_ratings(solo)
    assert solo[0].delta == 0
    assert solo[0].new_rating == 1450


def test_rating_never_falls_below_the_floor():
    field = [Competitor(1, 305, score=0.0)] + [
        Competitor(i, 2400, score=float(i * 10)) for i in range(2, 30)
    ]
    compute_ratings(field)
    assert all(c.new_rating >= 300 for c in field)
    # The recorded delta must match what actually happened after the floor.
    assert all(c.new_rating == c.rating + c.delta for c in field)


def test_performance_rating_is_clamped_to_a_defensible_band():
    """Rank 1 has no exact solution, so it must not report the solver's ceiling."""
    others = [1500] * 20
    assert performance_rating(1, others) == 1900  # max(others) + 400
    assert performance_rating(20, others) == 1100  # min(others) - 400
    assert performance_rating(1, []) == 1200


def test_expected_percentile_tracks_relative_strength():
    field = [1200] * 30
    assert expected_score_fraction(2000, field) > 0.8
    assert expected_score_fraction(600, field) < 0.3


@pytest.mark.parametrize(
    "rating,expected",
    [(2500, "legend"), (2200, "master"), (1950, "expert"),
     (1700, "specialist"), (1400, "apprentice"), (900, "novice")],
)
def test_tiers(rating, expected):
    assert tier_for(rating)[0] == expected


# --------------------------------------------------------------------------- #
# Markup
# --------------------------------------------------------------------------- #
def test_html_in_question_text_is_escaped_not_executed():
    out = render("Careful: <script>alert('xss')</script> and <img onerror=x>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_javascript_urls_are_rejected():
    out = render("[click](javascript:alert(1))")
    assert "javascript:alert" not in out.replace("&#58;", ":") or 'href="javascript' not in out
    assert "<a href" not in out


def test_data_uri_images_are_rejected():
    out = render("![x](data:text/html;base64,PHNjcmlwdD4=)")
    assert "<img" not in out


def test_same_origin_media_is_allowed():
    out = render("![Figure 1](/media/figures/9/a.png)")
    assert '<img src="/media/figures/9/a.png"' in out


def test_biology_notation():
    """Sub- and superscript matter more than general prose formatting here."""
    out = render("H~2~O and Ca^2+^ and **bold**")
    assert "<sub>2</sub>" in out
    assert "<sup>2+</sup>" in out
    assert "<strong>bold</strong>" in out


def test_pipe_tables_render():
    out = render("| Species | A |\n| --- | --- |\n| T. salsola | + |")
    assert "<table" in out and "<th>Species</th>" in out and "<td>+</td>" in out


def test_lists_render():
    out = render("- one\n- two")
    assert out.count("<li>") == 2


def test_empty_input_is_safe():
    assert render(None) == ""
    assert render("") == ""
    assert plain(None) == ""


def test_plain_truncates_with_an_ellipsis():
    excerpt = plain("word " * 100, limit=40)
    assert len(excerpt) <= 40
    assert excerpt.endswith("…")
