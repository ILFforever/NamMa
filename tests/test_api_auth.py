from tests.conftest import login


def test_demo_login_and_me(api):
    client, _ = api
    token = login(client, "responder")
    r = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["data"]["role"] == "responder"
    assert "ingested_at" in r.json()["meta"]


def test_me_needs_a_session(api):
    client, _ = api
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = client.get("/api/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


def test_bad_login_body_is_400(api):
    client, _ = api
    r = client.post("/api/auth/line", json={})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
