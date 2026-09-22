"""HTTP-level tests: the pages render, and the exam flow works end to end."""
from __future__ import annotations

from datetime import timedelta

from app.db import utcnow
from app.models.attempt import Attempt
from app.models.enums import AttemptStatus, ExamMode, QuestionStatus, Role
from app.models.user import User
from app.services import exam_engine
from tests.conftest import make_exam, make_question, make_topic, make_user, sign_in


def test_landing_page_renders_for_a_visitor(client):
    # The default locale is Russian, so assert on the brand and a localised string.
    response = client.get("/")
    assert response.status_code == 200
    assert "Платформа НБО" in response.text
    assert client.get("/?lang=en").status_code == 200


def test_healthcheck(client):
    assert client.get("/healthz").text == "ok"


def test_unknown_page_returns_a_rendered_404(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "404" in response.text


def test_locale_switch_changes_the_rendered_language(client):
    russian = client.get("/", headers={"accept-language": "ru"})
    english = client.get("/?lang=en")
    assert "Осваивайте биологию" in russian.text
    assert "Master biology" in english.text


def test_kyrgyz_locale_is_available(client):
    response = client.get("/?lang=ky")
    assert response.status_code == 200
    assert "Биологияны" in response.text


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_registration_creates_an_account_and_signs_in(client, db):
    response = client.post(
        "/auth/register",
        data={
            "email": "New.Student@example.test",
            "username": "newstudent",
            "full_name": "New Student",
            "password": "a-good-long-password",
            "password_confirm": "a-good-long-password",
            "school_id": "",
            "grade": "10",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    user = db.query(User).filter(User.username == "newstudent").one()
    assert user.email == "new.student@example.test"  # normalised
    assert user.role is Role.PARTICIPANT
    assert user.registration_ip  # recorded for the integrity trail


def test_registration_rejects_a_mismatched_password(client, db):
    response = client.post(
        "/auth/register",
        data={
            "email": "x@example.test", "username": "xx", "full_name": "X",
            "password": "a-good-long-password", "password_confirm": "different",
            "school_id": "", "grade": "",
        },
    )
    assert response.status_code == 400
    assert db.query(User).filter(User.username == "xx").one_or_none() is None


def test_registration_rejects_a_duplicate_email(client, db):
    make_user(db, "taken", email="taken@example.test")
    response = client.post(
        "/auth/register",
        data={
            "email": "taken@example.test", "username": "other", "full_name": "Other",
            "password": "a-good-long-password", "password_confirm": "a-good-long-password",
            "school_id": "", "grade": "",
        },
    )
    assert response.status_code == 400


def test_login_with_a_bad_password_fails(client, db):
    make_user(db, "student")
    response = client.post(
        "/auth/login", data={"login": "student", "password": "wrong", "next": "/"}
    )
    assert response.status_code == 400


def test_login_succeeds_and_sets_a_session(client, db):
    make_user(db, "student")
    response = client.post(
        "/auth/login",
        data={"login": "student", "password": "correct-horse-battery", "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "nbo_session" in response.cookies


def test_banned_account_cannot_sign_in(client, db):
    user = make_user(db, "banned")
    user.is_banned = True
    db.flush()
    response = client.post(
        "/auth/login",
        data={"login": "banned", "password": "correct-horse-battery", "next": "/"},
    )
    assert response.status_code == 403


def test_login_next_cannot_redirect_off_site(client, db):
    make_user(db, "student")
    response = client.post(
        "/auth/login",
        data={
            "login": "student",
            "password": "correct-horse-battery",
            "next": "https://evil.example/steal",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def test_stats_requires_signing_in(client):
    response = client.get("/stats", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


def test_participants_cannot_reach_creative_mode(client, db):
    sign_in(client, make_user(db, "student"))
    assert client.get("/creative").status_code == 403


def test_authors_can_reach_creative_mode(client, db):
    sign_in(client, make_user(db, "writer", role=Role.AUTHOR))
    assert client.get("/creative").status_code == 200


def test_non_admins_cannot_reach_admin(client, db):
    sign_in(client, make_user(db, "writer", role=Role.AUTHOR))
    assert client.get("/admin").status_code == 403


def test_a_participant_cannot_open_someone_elses_attempt(client, db):
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question])
    owner = make_user(db, "owner")
    attempt = exam_engine.start_attempt(db, owner, exam, ip="91.213.10.1", fingerprint="fp1")

    sign_in(client, make_user(db, "nosy"), fingerprint="fp2")
    assert client.get(f"/survival/attempt/{attempt.id}").status_code == 403


# --------------------------------------------------------------------------- #
# Sitting an exam over HTTP
# --------------------------------------------------------------------------- #
def test_full_exam_flow(client, db):
    topic = make_topic(db)
    questions = [make_question(db, topic=topic) for _ in range(3)]
    exam = make_exam(db, questions, mode=ExamMode.PRACTICE, slug="flow-exam")
    user = make_user(db, "sitter")
    sign_in(client, user, fingerprint="fp-sitter")

    # The exam page offers a start button.
    detail = client.get("/survival/flow-exam")
    assert detail.status_code == 200

    start = client.post("/survival/flow-exam/start", follow_redirects=False)
    assert start.status_code == 303
    attempt_id = int(start.headers["location"].rsplit("/", 1)[-1])

    player = client.get(f"/survival/attempt/{attempt_id}")
    assert player.status_code == 200
    assert 'data-question-card' in player.text

    attempt = db.get(Attempt, attempt_id)
    for answer in attempt.answers:
        statements = answer.exam_question.question.statements
        saved = client.post(
            f"/survival/attempt/{attempt_id}/answer",
            json={
                "exam_question_id": answer.exam_question_id,
                "response": {str(s.id): s.is_true for s in statements},
                "seconds_spent": 42,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["ok"] is True

    state = client.get(f"/survival/attempt/{attempt_id}/state").json()
    assert state["progress"]["answered"] == 3
    assert state["seconds_remaining"] > 0

    submit = client.post(f"/survival/attempt/{attempt_id}/submit", follow_redirects=False)
    assert submit.status_code == 303

    db.expire_all()
    attempt = db.get(Attempt, attempt_id)
    assert attempt.status is AttemptStatus.SUBMITTED
    assert attempt.raw_score == 3.0

    result = client.get(f"/survival/attempt/{attempt_id}/result")
    assert result.status_code == 200
    assert "100" in result.text


def test_player_redirects_to_results_once_submitted(client, db):
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, slug="done-exam")
    user = make_user(db, "done")
    sign_in(client, user)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    exam_engine.submit_attempt(db, attempt)

    response = client.get(f"/survival/attempt/{attempt.id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/result")


def test_autosave_after_the_deadline_returns_a_conflict(client, db):
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, slug="late-exam")
    user = make_user(db, "late")
    sign_in(client, user)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    attempt.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()

    response = client.post(
        f"/survival/attempt/{attempt.id}/answer",
        json={"exam_question_id": attempt.answers[0].exam_question_id, "response": {}},
    )
    assert response.status_code == 409
    assert response.json()["redirect"].endswith("/result")


def test_results_are_withheld_until_the_window_closes(client, db):
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(
        db, [question], mode=ExamMode.OFFICIAL, slug="sealed-exam",
        closes_at=utcnow() + timedelta(hours=2),
    )
    user = make_user(db, "waiting")
    sign_in(client, user)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    exam_engine.submit_attempt(db, attempt)

    response = client.get(f"/survival/attempt/{attempt.id}/result")
    assert response.status_code == 200
    assert "⏳" in response.text  # the pending page, not the score


def test_telemetry_is_recorded(client, db):
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, slug="watched-exam")
    user = make_user(db, "watched")
    sign_in(client, user)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")

    response = client.post(
        f"/survival/attempt/{attempt.id}/event",
        json={"kind": "window_blur", "data": {}},
    )
    assert response.status_code == 200
    db.refresh(attempt)
    assert attempt.focus_losses >= 1


def test_unknown_telemetry_kind_is_rejected(client, db):
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, slug="strict-exam")
    user = make_user(db, "strict")
    sign_in(client, user)
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")

    response = client.post(
        f"/survival/attempt/{attempt.id}/event", json={"kind": "sudo_make_me_a_sandwich"}
    )
    assert response.status_code == 400


def test_blocked_start_shows_the_denial_page_and_records_it(client, db):
    from app.models.security import ExamAccessDenial

    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], slug="guarded-exam")
    # The server salts and hashes the cookie value before storing it, so the
    # seeded attempt has to carry the hashed form to collide with the request.
    from app.services.auth import hash_fingerprint

    first = make_user(db, "first")
    exam_engine.start_attempt(
        db, first, exam, ip="91.213.10.1", fingerprint=hash_fingerprint("shared-fp")
    )
    db.commit()

    second = make_user(db, "second")
    sign_in(client, second, fingerprint="shared-fp")
    response = client.post("/survival/guarded-exam/start")

    assert response.status_code == 403
    assert db.query(ExamAccessDenial).filter(ExamAccessDenial.user_id == second.id).count() == 1


# --------------------------------------------------------------------------- #
# Creative mode
# --------------------------------------------------------------------------- #
def test_author_can_create_and_edit_a_question(client, db):
    from app.models.content import Question

    topic = make_topic(db)
    sign_in(client, make_user(db, "writer", role=Role.AUTHOR))

    created = client.post(
        "/creative/questions/new",
        data={"question_type": "tf_block", "topic_id": str(topic.id)},
        follow_redirects=False,
    )
    assert created.status_code == 303
    question_id = int(created.headers["location"].rsplit("/", 1)[-1])

    question = db.get(Question, question_id)
    assert len(question.statements) == 4  # IBO default block
    assert question.status is QuestionStatus.DRAFT

    editor = client.get(f"/creative/questions/{question_id}")
    assert editor.status_code == 200

    payload = {
        "topic_id": str(topic.id), "difficulty": "hard", "question_type": "tf_block",
        "max_points": "1.0", "estimated_seconds": "200",
        "source_ref": "IBO 2024 Theory A Q1", "source_year": "2024",
        "tags": "parsimony, tulipa", "scoring_curve": "",
    }
    for locale in ("ru", "ky", "en"):
        payload[f"stem__{locale}"] = f"Stem in {locale}"
    for index, statement in enumerate(question.statements):
        for locale in ("ru", "ky", "en"):
            payload[f"st__{statement.id}__text__{locale}"] = f"Statement {index} {locale}"
            payload[f"st__{statement.id}__expl__{locale}"] = "Because."
        if index % 2 == 0:
            payload[f"st__{statement.id}__true"] = "on"

    saved = client.post(f"/creative/questions/{question_id}", data=payload,
                        follow_redirects=False)
    assert saved.status_code == 303

    db.expire_all()
    question = db.get(Question, question_id)
    assert question.tr("ru").stem == "Stem in ru"
    assert question.source_ref == "IBO 2024 Theory A Q1"
    assert [s.is_true for s in question.statements] == [True, False, True, False]
    assert question.tags == ["parsimony", "tulipa"]


def test_submitting_an_incomplete_question_reports_the_problems(client, db):
    from app.models.content import Question

    sign_in(client, make_user(db, "writer", role=Role.AUTHOR))
    created = client.post(
        "/creative/questions/new", data={"question_type": "tf_block", "topic_id": ""},
        follow_redirects=False,
    )
    question_id = int(created.headers["location"].rsplit("/", 1)[-1])

    response = client.post(f"/creative/questions/{question_id}/submit")
    assert response.status_code == 400
    assert "topic" in response.text.lower()
    assert db.get(Question, question_id).status is QuestionStatus.DRAFT


def test_cloning_a_template_keeps_provenance(client, db):
    from app.models.content import Question

    topic = make_topic(db)
    original = make_question(db, topic=topic, stem="Original IBO stem")
    original.is_official_ibo = True
    original.source_ref = "IBO 2024 Theory A Q7"
    db.flush()

    sign_in(client, make_user(db, "writer", role=Role.AUTHOR))
    response = client.post(f"/creative/questions/{original.id}/clone", follow_redirects=False)
    assert response.status_code == 303
    clone_id = int(response.headers["location"].rsplit("/", 1)[-1])

    clone = db.get(Question, clone_id)
    assert clone.id != original.id
    assert clone.template_of_id == original.id
    assert clone.is_official_ibo is False  # a derived question is not the original
    assert clone.source_ref == original.source_ref
    assert len(clone.statements) == len(original.statements)
    assert clone.tr("ru").stem == "Original IBO stem"


def test_reviewer_can_approve_a_question(client, db):
    from app.models.content import Question

    topic = make_topic(db)
    question = make_question(db, topic=topic)
    question.status = QuestionStatus.IN_REVIEW
    db.flush()

    sign_in(client, make_user(db, "checker", role=Role.REVIEWER))
    assert client.get("/creative/review").status_code == 200

    response = client.post(
        f"/creative/questions/{question.id}/review",
        data={"decision": "approve", "comment": "Looks right."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Question, question.id).status is QuestionStatus.APPROVED


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def test_admin_pages_render(client, db):
    sign_in(client, make_user(db, "boss", role=Role.ADMIN))
    for path in ("/admin", "/admin/integrity", "/admin/denials",
                 "/admin/networks", "/admin/users", "/admin/schools"):
        assert client.get(path).status_code == 200, path


def test_admin_can_void_and_restore_an_attempt(client, db):
    question = make_question(db, topic=make_topic(db))
    exam = make_exam(db, [question], mode=ExamMode.PRACTICE, slug="void-exam")
    user = make_user(db, "sitter")
    attempt = exam_engine.start_attempt(db, user, exam, ip="91.213.10.1", fingerprint="fp")
    exam_engine.submit_attempt(db, attempt)

    sign_in(client, make_user(db, "boss", role=Role.ADMIN))
    client.post(f"/admin/attempts/{attempt.id}/void",
                data={"reason": "confirmed collusion"}, follow_redirects=False)
    db.expire_all()
    assert db.get(Attempt, attempt.id).status is AttemptStatus.VOIDED

    client.post(f"/admin/attempts/{attempt.id}/restore", follow_redirects=False)
    db.expire_all()
    assert db.get(Attempt, attempt.id).status is AttemptStatus.SUBMITTED


def test_admin_cannot_demote_the_last_admin(client, db):
    boss = make_user(db, "boss", role=Role.ADMIN)
    sign_in(client, boss)
    response = client.post(
        f"/admin/users/{boss.id}", data={"action": "set_role", "role": "participant"}
    )
    assert response.status_code == 400
    db.expire_all()
    assert db.get(User, boss.id).role is Role.ADMIN


def test_admin_can_register_a_school_network(client, db):
    from app.models.user import School

    sign_in(client, make_user(db, "boss", role=Role.ADMIN))
    response = client.post(
        "/admin/schools",
        data={
            "school_id": "", "name": "Lyceum 61", "city": "Bishkek", "region": "Chuy",
            "cidrs": "212.42.101.0/24\n212.42.102.0/24", "max_from_ip": "40",
            "is_verified": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    school = db.query(School).filter(School.name == "Lyceum 61").one()
    assert school.allowlisted_cidrs == ["212.42.101.0/24", "212.42.102.0/24"]
    assert school.is_verified is True


def test_a_malformed_cidr_is_rejected(client, db):
    sign_in(client, make_user(db, "boss", role=Role.ADMIN))
    response = client.post(
        "/admin/schools",
        data={"school_id": "", "name": "Bad", "city": "", "region": "",
              "cidrs": "not-a-network", "max_from_ip": "40", "is_verified": ""},
    )
    assert response.status_code == 400
