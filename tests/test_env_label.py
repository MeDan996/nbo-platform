from app.config import settings


def test_banner_hidden_in_production(client):
    page = client.get("/")
    assert "env-banner" not in page.text
    assert 'name="robots"' not in page.text


def test_banner_and_noindex_when_labelled(client, monkeypatch):
    monkeypatch.setattr(settings, "env_label", "dev")
    page = client.get("/")
    assert 'class="env-banner"' in page.text and "DEV" in page.text
    assert 'content="noindex, nofollow"' in page.text


def test_bootstrap_admin_uses_configured_name(db, monkeypatch):
    from app.config import settings as s
    from app.db import SessionLocal
    import app.main as main
    from app.models.user import User

    # Run the bootstrap against the test database's session factory.
    monkeypatch.setattr(s, "bootstrap_admin_email", "boss@example.test")
    monkeypatch.setattr(s, "bootstrap_admin_password", "a-long-test-password")
    monkeypatch.setattr(s, "bootstrap_admin_username", "admin")
    monkeypatch.setattr(s, "bootstrap_admin_first_name", "Dayan")
    monkeypatch.setattr(s, "bootstrap_admin_last_name", "Melisov")
    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db)
    main._bootstrap_admin()
    user = db.query(User).filter(User.email == "boss@example.test").one()
    assert (user.username, user.first_name, user.last_name) == ("admin", "Dayan", "Melisov")
    assert user.full_name == "Dayan Melisov"
