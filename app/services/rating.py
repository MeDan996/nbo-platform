"""Contest rating.

Olympiads are many-player events, not a series of 1v1 games, so a plain Elo
update does not fit. This is the Codeforces rating algorithm, which was designed
exactly for this shape: every participant is treated as having played every other
participant, the expected finishing position ("seed") is compared against the
actual finishing position, and the whole field is then renormalised so the
contest neither inflates nor deflates the rating pool.

Reference behaviour:
  * ``P(j beats i) = 1 / (1 + 10 ** ((R_i - R_j) / 400))``
  * ``seed_i = 1 + sum over j != i of P(j beats i)``
  * target rank ``m_i = sqrt(seed_i * rank_i)`` (geometric mean of expected and actual)
  * ``need_i`` = the rating at which ``seed_i(need_i) == m_i``, found by bisection
  * ``d_i = (need_i - R_i) / 2``, then two sum-correction passes
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt

DEFAULT_RATING = 1200
RATING_FLOOR = 300


@dataclass
class Competitor:
    user_id: int
    rating: int
    score: float
    # Filled in by `compute_ratings`.
    rank: int = 0
    seed: float = 0.0
    delta: int = 0
    new_rating: int = 0
    performance: int = 0
    tiebreak: float = 0.0  # lower is better; used only to order equal scores


def _win_probability(rating_a: int | float, rating_b: int | float) -> float:
    """Probability that the competitor rated `rating_b` finishes above `rating_a`."""
    return 1.0 / (1.0 + 10.0 ** ((rating_a - rating_b) / 400.0))


def _seed(rating: float, others: list[int]) -> float:
    """Expected finishing position of a competitor at `rating` against `others`."""
    return 1.0 + sum(_win_probability(rating, other) for other in others)


def _clamp_performance(raw: float, others: list[int]) -> int:
    """Keep a reported performance rating inside a defensible band.

    The seed function has no solution for rank 1 against a finite field - the
    expected rank approaches but never reaches 1.0 - so the bisection runs to its
    ceiling and the winner's performance comes back as 8000. The same happens in
    reverse for last place. Capping at 400 points beyond the strongest (or
    weakest) competitor present is the conventional fix: a 400-point gap already
    means a ~91% expected win rate, which is as much as one contest can evidence.
    """
    if not others:
        return DEFAULT_RATING
    return int(round(max(min(raw, max(others) + 400), min(others) - 400)))


def _rating_for_seed(target_seed: float, others: list[int]) -> float:
    """Bisect for the rating whose expected rank equals `target_seed`.

    `_seed` decreases monotonically as rating rises, so bisection is exact to
    within the tolerance and needs no derivative.
    """
    low, high = 1.0, 8000.0
    for _ in range(60):
        mid = (low + high) / 2
        if _seed(mid, others) < target_seed:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def assign_ranks(competitors: list[Competitor]) -> None:
    """Competition ranking on score, descending. Ties share the best rank
    (1, 2, 2, 4), which is how olympiad standings are published."""
    ordered = sorted(competitors, key=lambda c: (-c.score, c.tiebreak))
    rank = 0
    previous_key: tuple | None = None
    for index, competitor in enumerate(ordered, start=1):
        key = (competitor.score,)
        if key != previous_key:
            rank = index
            previous_key = key
        competitor.rank = rank


def compute_ratings(competitors: list[Competitor]) -> list[Competitor]:
    """Run one rated contest. Mutates and returns the competitors in place.

    A field of fewer than two competitors cannot produce a meaningful comparison,
    so ratings are left untouched.
    """
    n = len(competitors)
    if n == 0:
        return competitors
    if n == 1:
        only = competitors[0]
        only.rank = 1
        only.seed = 1.0
        only.delta = 0
        only.new_rating = only.rating
        only.performance = only.rating
        return competitors

    assign_ranks(competitors)
    ratings = [c.rating for c in competitors]

    # Step 1: seed and provisional delta.
    deltas: list[float] = []
    for index, competitor in enumerate(competitors):
        others = ratings[:index] + ratings[index + 1 :]
        competitor.seed = _seed(competitor.rating, others)
        target = sqrt(competitor.seed * competitor.rank)
        need = _rating_for_seed(target, others)
        competitor.performance = _clamp_performance(
            _rating_for_seed(float(competitor.rank), others), others
        )
        deltas.append((need - competitor.rating) / 2.0)

    # Step 2: the whole field's deltas must sum to slightly below zero, so the
    # rating pool does not inflate as more contests are held.
    total = sum(deltas)
    shift = -total / n - 1.0
    deltas = [d + shift for d in deltas]

    # Step 3: the top sqrt-sized cohort is held closer to zero-sum, which stops
    # a weak field from handing out free rating at the top of the table.
    top_count = min(n, int(round(4 * sqrt(n))))
    by_rating = sorted(range(n), key=lambda i: -competitors[i].rating)[:top_count]
    top_sum = sum(deltas[i] for i in by_rating)
    shift = max(min(-top_sum / top_count, 0.0), -10.0)
    deltas = [d + shift for d in deltas]

    for competitor, delta in zip(competitors, deltas):
        rounded = int(round(delta))
        new_rating = max(RATING_FLOOR, competitor.rating + rounded)
        # Re-derive the delta after the floor so history stays self-consistent.
        competitor.delta = new_rating - competitor.rating
        competitor.new_rating = new_rating

    return competitors


def performance_rating(rank: int, other_ratings: list[int]) -> int:
    """Rating a competitor would need for `rank` to be their expected finish."""
    if not other_ratings:
        return DEFAULT_RATING
    return _clamp_performance(_rating_for_seed(float(rank), other_ratings), other_ratings)


def expected_score_fraction(rating: int, field: list[int]) -> float:
    """Expected percentile (0..1) of a competitor within a field. Shown on the
    pre-exam screen as 'you are typically around the Nth percentile here'."""
    if not field:
        return 0.5
    expected_rank = _seed(rating, field)
    return max(0.0, min(1.0, 1.0 - (expected_rank - 1) / max(1, len(field))))


TIER_THRESHOLDS: list[tuple[int, str, str]] = [
    (2400, "legend", "#8b2fc9"),
    (2100, "master", "#e8622d"),
    (1900, "expert", "#1865f2"),
    (1600, "specialist", "#0f9d58"),
    (1350, "apprentice", "#00a2ae"),
    (0, "novice", "#6b7280"),
]


def tier_for(rating: int) -> tuple[str, str]:
    for threshold, name, color in TIER_THRESHOLDS:
        if rating >= threshold:
            return name, color
    return "novice", "#6b7280"


@dataclass
class RatingPreview:
    """What the participant sees before a rated exam opens."""

    rating: int
    tier: str = field(default="")
    color: str = field(default="")

    def __post_init__(self) -> None:
        self.tier, self.color = tier_for(self.rating)
