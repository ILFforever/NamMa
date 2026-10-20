"""LINE web login: /api/auth/line/start → LINE → /api/auth/line/callback → /api/auth/line/claim (LINE faked)."""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tests.conftest import login


class FakeLine:
    """api.line.me: the token endpoint gives an ID token, verify checks the nonce and gives the LINE user."""

    def __init__(self, sub="Uline123"):
        self.sub, self.calls = sub, []

    def __call__(self, url, data=None, **_):
        self.calls.append((url, data))
        if url.endswith("/token"):
            return httpx.Response(200, json={"id_token": "line-id-token", "access_token": "a"})
        assert data["nonce"] == self.nonce
        return httpx.Response(200, json={"sub": self.sub, "name": "น้ำ", "picture": None})


@pytest.fixture()
def line(api, monkeypatch):
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "1234")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_SECRET", "s3cret")
    fake = FakeLine()
    monkeypatch.setattr("api.auth.httpx.post", fake)
    return api[0], api[1], fake


def start(client, fake, **body):
    r = client.post("/api/auth/line/start", json=body)
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["data"]["authorize_url"]).query)
    fake.nonce = q["nonce"][0]
    return q


def back(r):
    """The callback's redirect: (path, its query, fragment)."""
    assert r.status_code == 303
    u = urlparse(r.headers["location"])
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}, u.fragment


def test_signs_in_through_line_in_the_same_browser(line):
    client, conn, fake = line
    q = start(client, fake, chat_session="browser-1")
    assert q["client_id"] == ["1234"] and q["scope"] == ["openid profile"] and q["disable_auto_login"] == ["true"]
    assert q["redirect_uri"] == ["http://testserver/api/auth/line/callback"]

    path, query, frag = back(client.get("/api/auth/line/callback", params={"code": "c", "state": q["state"][0]},
                                        follow_redirects=False))
    assert (path, frag) == ("/", "/account") and "line_claim" in query
    token_call = fake.calls[0]
    assert token_call[1]["client_secret"] == "s3cret" and token_call[1]["code"] == "c"

    r = client.post("/api/auth/line/claim", json={"claim": query["line_claim"]})
    assert r.status_code == 200
    token = r.json()["data"]["session_token"]
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()["data"]
    assert me["identities"]["line"] is True
    assert conn.execute("SELECT 1 FROM users WHERE line_user_id = 'Uline123'").fetchone()

    # The claim works once.
    assert client.post("/api/auth/line/claim", json={"claim": query["line_claim"]}).status_code == 401


def test_refuses_line_coming_back_in_another_browser(line):
    client, _, fake = line
    q = start(client, fake)
    client.cookies.clear()   # LINE opened the callback in a browser that never started this sign-in

    _, query, _ = back(client.get("/api/auth/line/callback", params={"code": "c", "state": q["state"][0]},
                                  follow_redirects=False))
    assert query == {"line_error": "browser"} and fake.calls == []


def test_unknown_state_and_cancel(line):
    client, _, fake = line
    _, query, _ = back(client.get("/api/auth/line/callback", params={"code": "c", "state": "nope"},
                                  follow_redirects=False))
    assert query == {"line_error": "expired"}

    q = start(client, fake)
    _, query, _ = back(client.get("/api/auth/line/callback",
                                  params={"error": "access_denied", "state": q["state"][0]}, follow_redirects=False))
    assert query == {"line_error": "cancelled"}


def test_links_line_to_the_signed_in_account(line):
    client, conn, fake = line
    r = client.post("/api/auth/google", json={"id_token": "demo:google:mali"})
    token = r.json()["data"]["session_token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/line/start", json={"intent": "link"}).status_code == 401   # link needs a session

    r = client.post("/api/auth/line/start", json={"intent": "link"}, headers=auth)
    fake.nonce = parse_qs(urlparse(r.json()["data"]["authorize_url"]).query)["nonce"][0]
    state = parse_qs(urlparse(r.json()["data"]["authorize_url"]).query)["state"][0]
    _, query, _ = back(client.get("/api/auth/line/callback", params={"code": "c", "state": state},
                                  follow_redirects=False))
    user = client.post("/api/auth/line/claim", json={"claim": query["line_claim"]}).json()["data"]["user"]
    assert user["display_name"] == "mali" and user["identities"] == {"line": True, "google": "mali@demo.namma"}


def test_linking_a_line_account_that_conflicts_comes_back_as_conflict(line):
    client, _, fake = line
    auth = {"Authorization": f"Bearer {login(client, 'other')}"}   # already has a (demo) LINE id
    r = client.post("/api/auth/line/start", json={"intent": "link"}, headers=auth)
    q = parse_qs(urlparse(r.json()["data"]["authorize_url"]).query)
    fake.nonce = q["nonce"][0]
    _, query, _ = back(client.get("/api/auth/line/callback", params={"code": "c", "state": q["state"][0]},
                                  follow_redirects=False))
    assert query == {"line_error": "conflict"}


def test_unconfigured_is_503(api, monkeypatch):
    client, _ = api
    monkeypatch.delenv("LINE_LOGIN_CHANNEL_SECRET", raising=False)
    assert client.post("/api/auth/line/start", json={}).status_code == 503
