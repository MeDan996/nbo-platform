from app.models.media import MediaFile

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_media_served_from_database(client, db):
    db.add(MediaFile(path="figures/1/a.png", mime_type="image/png", size=len(PNG), data=PNG))
    db.commit()
    resp = client.get("/media/figures/1/a.png")
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_media_missing_is_404(client):
    assert client.get("/media/figures/1/nope.png").status_code == 404
    assert client.get("/media/../config.py").status_code == 404
