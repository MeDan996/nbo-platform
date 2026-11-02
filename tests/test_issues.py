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


def test_reporter_shown_by_account_name(client, db):
    admin = make_user(db, "root", Role.ADMIN, full_name="Administrator")
    db.add(Issue(description="y", issue_type=IssueType.BUG, reporter_id=admin.id))
    db.commit()
    # Viewed by someone else, so the viewer's own header does not show the name.
    sign_in(client, make_user(db, "viewer", Role.AUTHOR, full_name="Some Viewer"))
    page = client.get("/issues")
    assert "root" in page.text
    assert "Administrator" not in page.text


def test_filters(client, db):
    author = make_user(db, "tester4", Role.AUTHOR)
    db.add(Issue(description="a bug one", issue_type=IssueType.BUG, reporter_id=author.id))
    db.add(Issue(description="a feature one", issue_type=IssueType.FEATURE, reporter_id=author.id))
    db.commit()
    sign_in(client, author)
    page = client.get("/issues?type=feature")
    assert "a feature one" in page.text and "a bug one" not in page.text
