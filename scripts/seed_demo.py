"""Create demo accounts, schools and a couple of simulated sittings.

Intended for a fresh checkout or a demo environment - it makes the dashboards,
leaderboard, statistics and integrity queue show something real instead of empty
states. Safe to re-run: every object is looked up before it is created.

    python scripts/seed_demo.py
    python scripts/seed_demo.py --attempts 60    # bigger simulated cohort

The passwords below are deliberately obvious. They are for a local demo; do not
run this against anything reachable from the internet.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, SessionLocal, engine, utcnow  # noqa: E402
from app.models.attempt import Attempt, AttemptAnswer  # noqa: E402
from app.models.enums import AttemptStatus, ExamMode, ExamStatus, Role  # noqa: E402
from app.models.exam import Exam  # noqa: E402
from app.models.user import School, User  # noqa: E402
from app.services import anticheat, exam_engine, stats  # noqa: E402
from app.services.auth import hash_password  # noqa: E402

DEMO_PASSWORD = "nbo-demo-2024"

SCHOOLS = [
    ("Bishkek Physics and Mathematics Lyceum #61", "Bishkek", "Chuy", ["212.42.101.0/24"]),
    ("Osh Gymnasium #38", "Osh", "Osh", ["213.145.64.0/22"]),
    ("Karakol Lyceum #5", "Karakol", "Issyk-Kul", []),
]

STAFF = [
    ("admin@nbo.kg", "admin", "Aigul Osmonova", Role.ADMIN),
    ("reviewer@nbo.kg", "reviewer", "Bakyt Sultanov", Role.REVIEWER),
    ("author@nbo.kg", "author", "Cholpon Abdyrakhmanova", Role.AUTHOR),
]

FIRST_NAMES = [
    "Aidai", "Beksultan", "Cholpon", "Daniyar", "Elnura", "Farrukh", "Gulnara",
    "Islam", "Jyldyz", "Kanat", "Leila", "Meerim", "Nurbek", "Omurbek", "Perizat",
    "Ruslan", "Saltanat", "Timur", "Ulan", "Venera", "Zhanybek", "Aizada",
    "Bermet", "Dastan", "Erkin", "Gulzat", "Iskender", "Kubanych", "Nazira", "Samat",
]
LAST_NAMES = [
    "Abdyldaev", "Bekturov", "Dzhumabaev", "Esenov", "Ismailov", "Kadyrov",
    "Mambetov", "Nurlanov", "Orozbekov", "Sadykov", "Toktogulov", "Usenov",
]


def seed_schools(db) -> list[School]:
    rows = []
    for name, city, region, cidrs in SCHOOLS:
        school = db.query(School).filter(School.name == name).one_or_none()
        if school is None:
            school = School(name=name)
            db.add(school)
            db.flush()
        school.city = city
        school.region = region
        school.allowlisted_cidrs = cidrs
        school.is_verified = bool(cidrs)
        rows.append(school)
    db.flush()
    return rows


def ensure_user(db, email, username, full_name, role, school=None, grade=None) -> User:
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        user = User(
            email=email,
            username=username,
            full_name=full_name,
            password_hash=hash_password(DEMO_PASSWORD),
            role=role,
        )
        db.add(user)
        db.flush()
    user.role = role
    user.school_id = school.id if school else None
    user.grade = grade
    return user


def seed_participants(db, schools, count: int) -> list[User]:
    rng = random.Random(20240711)
    participants: list[User] = []
    for index in range(count):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        username = f"{first.lower()}{index:03d}"
        user = ensure_user(
            db,
            email=f"{username}@example.kg",
            username=username,
            full_name=f"{first} {last}",
            role=Role.PARTICIPANT,
            school=rng.choice(schools),
            grade=rng.choice([9, 10, 11]),
        )
        participants.append(user)
    db.flush()
    return participants


def simulate_sitting(db, exam: Exam, user: User, ability: float, rng: random.Random,
                     ip: str, fingerprint: str) -> Attempt | None:
    """Play one participant through the paper.

    `ability` is the per-statement probability of answering correctly, so the
    resulting score distribution has the shape the IBO curve produces in
    practice: a long left tail, because three-of-four earns only 0.6.
    """
    existing = (
        db.query(Attempt)
        .filter(Attempt.exam_id == exam.id, Attempt.user_id == user.id)
        .first()
    )
    if existing is not None:
        return None

    attempt = exam_engine.start_attempt(
        db, user, exam, ip=ip, fingerprint=fingerprint, user_agent="seed/1.0"
    )
    for answer in attempt.answers:
        exam_question = answer.exam_question
        statements = exam_question.question.statements
        response = {}
        for statement in statements:
            if rng.random() < 0.04:
                continue  # left blank
            correct = rng.random() < ability
            response[str(statement.id)] = statement.is_true if correct else (not statement.is_true)
        answer.response = response
        answer.seconds_spent = max(8, int(rng.gauss(150, 60)))
        answer.answered_at = utcnow()

    attempt.started_at = utcnow() - timedelta(minutes=rng.randint(70, 190))
    exam_engine.submit_attempt(db, attempt, auto=False, ip=ip)
    return attempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=40, help="simulated participants")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    rng = random.Random(7)

    try:
        schools = seed_schools(db)
        for email, username, name, role in STAFF:
            ensure_user(db, email, username, name, role, school=schools[0])
        participants = seed_participants(db, schools, args.attempts)
        db.commit()

        exam = db.query(Exam).filter(Exam.slug == "ibo-2024-theory-a").one_or_none()
        if exam is None:
            print("No imported exam found - run scripts/import_ibo.py first.")
            return 1

        # Make it a rated, closed sitting so the leaderboard and rating history
        # have something in them.
        exam.mode = ExamMode.OFFICIAL
        exam.is_rated = True
        exam.results_visible_at = utcnow()
        db.flush()

        print(f"simulating {len(participants)} sittings…")
        for index, user in enumerate(participants):
            ability = min(0.97, max(0.4, rng.gauss(0.68, 0.12)))

            # Most participants sit from their own connection. A handful share a
            # school lab address (allowlisted, so permitted), and one pair
            # deliberately shares a device so the integrity queue has a real
            # critical flag to show.
            if index < 6:
                ip, fingerprint = "212.42.101.17", f"seedfp{index:04d}"
            elif index in (7, 8):
                ip, fingerprint = "95.47.12.200", "seedfp-shared-device"
            else:
                ip = f"95.47.{rng.randint(1, 250)}.{rng.randint(1, 250)}"
                fingerprint = f"seedfp{index:04d}"

            anticheat.touch_ip(db, ip, user)
            anticheat.touch_device(db, fingerprint, user)
            simulate_sitting(db, exam, user, ability, rng, ip, fingerprint)
            if index % 10 == 0:
                db.commit()
        db.commit()

        report = exam_engine.finalise_exam(db, exam, apply_ratings=True)
        db.commit()

        stats.compute_exam_statistics(db, exam)
        db.commit()

        print(
            f"  participants={report.participants} "
            f"rating_changes={report.rating_changes} flagged={report.flagged_attempts}"
        )
        print()
        print("Sign in with any of these (password: %s)" % DEMO_PASSWORD)
        for email, username, name, role in STAFF:
            print(f"  {role.value:11} {email}")
        print(f"  participant {participants[0].email}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
