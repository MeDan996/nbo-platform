from app.models.enums import IssueStatus, IssueType, Role
from app.models.issue import Issue
from tests.conftest import make_user, sign_in


def test_participant_cannot_see_issues(client, db):
    student = make_user(db, "stud", Role.PARTICIPANT)
    sign_in(client, student)
    assert client.get("/issues").status_code == 403


def test_staff_can_report_and_list(client, db):
    author = make_user(db, "tester1", Role.AUTHOR, full_name="Test Er")
    sign_in(client, author)
    resp = client.post(
        "/issues",
        data={"description": "Save button is hidden on phones", "issue_type": "bug"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    issue = db.query(Issue).one()
    assert issue.id == 1
    assert issue.issue_type is IssueType.BUG
    assert issue.status is IssueStatus.SUBMITTED
    assert issue.reporter_id == author.id
    page = client.get("/issues")
    assert page.status_code == 200
    assert "Save button is hidden on phones" in page.text
    assert "tester1" in page.text


def test_blank_description_rejected(client, db):
    sign_in(client, make_user(db, "tester2", Role.AUTHOR))
    resp = client.post("/issues", data={"description": "   ", "issue_type": "bug"})
    assert resp.status_code == 400
    assert db.query(Issue).count() == 0


def test_any_staff_can_change_status(client, db):
    author = make_user(db, "tester3", Role.AUTHOR)
    db.add(Issue(description="x", issue_type=IssueType.FEATURE, reporter_id=author.id))
    db.commit()

    sign_in(client, author)
    resp = client.post("/issues/1/status", data={"status": "working_on"}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Issue, 1).status is IssueStatus.WORKING_ON
    assert client.post("/issues/1/status", data={"status": "nonsense"}).status_code == 400

    sign_in(client, make_user(db, "stud2", Role.PARTICIPANT))
    assert client.post("/issues/1/status", data={"status": "done"}).status_code == 403


def test_reporter_shown_by_first_and_last_name(client, db):
    admin = make_user(
        db, "root", Role.ADMIN, full_name="Administrator", first_name="Dayan", last_name="Melisov"
    )
    db.add(Issue(description="y", issue_type=IssueType.BUG, reporter_id=admin.id))
    nameless = make_user(db, "nameless", Role.AUTHOR, full_name="Old Style")
    db.add(Issue(description="z", issue_type=IssueType.BUG, reporter_id=nameless.id))
    db.commit()
    # Viewed by someone else, so the viewer's own header does not show a name.
    sign_in(client, make_user(db, "viewer", Role.AUTHOR, full_name="Some Viewer"))
    page = client.get("/issues")
    assert "Dayan Melisov" in page.text
    assert "Administrator" not in page.text
    # No name parts on file: fall back to the username.
    assert "nameless" in page.text


def test_filters(client, db):
    author = make_user(db, "tester4", Role.AUTHOR)
    db.add(Issue(description="a bug one", issue_type=IssueType.BUG, reporter_id=author.id))
    db.add(Issue(description="a feature one", issue_type=IssueType.FEATURE, reporter_id=author.id))
    db.commit()
    sign_in(client, author)
    page = client.get("/issues?type=feature")
    assert "a feature one" in page.text and "a bug one" not in page.text


def test_username_login_and_name_parts(client, db):
    from app.models.user import User
    from app.services.auth import hash_password

    user = User(
        email="dayan.melisov@gmail.com",
        username="admin",
        first_name="Dayan",
        last_name="Melisov",
        full_name="",
        password_hash=hash_password("correct-horse-battery"),
        role=Role.ADMIN,
    )
    db.add(user)
    db.commit()
    assert user.full_name == "Dayan Melisov"
    for login in ("admin", "ADMIN", "dayan.melisov@gmail.com"):
        resp = client.post(
            "/auth/login",
            data={"login": login, "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, login
        client.cookies.clear()
