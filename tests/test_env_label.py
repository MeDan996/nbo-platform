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
