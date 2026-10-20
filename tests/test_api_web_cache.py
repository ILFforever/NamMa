"""The built web app's cache headers: pages revalidate on every load, hashed assets are cached for good."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import WebFiles


def test_pages_revalidate_and_assets_are_immutable(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>NamMa</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    app = FastAPI()
    app.mount("/", WebFiles(directory=tmp_path, html=True), name="web")
    client = TestClient(app)

    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/index.html").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/assets/index-abc123.js").headers["cache-control"]
