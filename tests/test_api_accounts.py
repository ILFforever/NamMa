"""Accounts (docs/api.md → Auth): LINE and Google sign-in, one memory per account, the anonymous web chat joining
the account on sign-in, linking LINE and Google, unlink, logout and PDPA erasure.

Uses demo tokens (DEMO_MODE=1, the default): "demo:user:<name>" (LINE) and "demo:google:<name>" (Google).
Skipped when no Postgres is reachable (see the api fixture in conftest). Network calls are disabled.
"""
import uuid

from tests.test_api_chat import _chat, seeded  # noqa: F401  (seeded is a fixture)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client, provider: str, id_token: str, chat_session: str | None = None) -> dict:
    body = {"id_token": id_token} | ({"chat_session": chat_session} if chat_session else {})
    r = client.post(f"/api/auth/{provider}", json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _profile(client, token: str) -> dict:
    return client.get("/api/profile", headers=_auth(token)).json()["data"]


def _say(client, token: str, message: str) -> dict:
    r = client.post("/api/chat", json={"session_id": str(uuid.uuid4()), "message": message}, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_each_person_gets_their_own_account_and_memory(seeded):
    client, _ = seeded
    a = _sign_in(client, "line", "demo:user:somchai")
    b = _sign_in(client, "line", "demo:user:malee")
    assert a["user"]["display_name"] == "somchai" and a["user"]["identities"] == {"line": True, "google": None}

    _say(client, a["session_token"], "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม")
    assert [p["label"] for p in _profile(client, a["session_token"])["places"]] == ["บ้านแม่"]
    assert _profile(client, b["session_token"])["places"] == []

    # Memory follows the account, not the browser tab: a new sign-in (another device) sees it.
    again = _sign_in(client, "line", "demo:user:somchai")
    assert [p["label"] for p in _profile(client, again["session_token"])["places"]] == ["บ้านแม่"]


def test_signing_in_folds_the_anonymous_chat_into_the_account(seeded):
    client, conn = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    _chat(client, {"session_id": sid, "message": "ยายติดเตียง"})
    _chat(client, {"session_id": sid, "postback": "consent:sensitive_memory:yes"})

    d = _sign_in(client, "line", "demo:user:anong", chat_session=sid)
    assert d["merged_chat"] is True
    p = _profile(client, d["session_token"])
    assert [x["label"] for x in p["places"]] == ["บ้านแม่"]
    assert p["consent"]["granted"] is True and any(f["kind"] == "household" for f in p["facts"])
    # The anonymous chat user is gone; its messages now belong to the account.
    assert conn.execute("SELECT count(*) FROM users WHERE line_user_id = %s", (f"web:{sid}",)).fetchone()[0] == 0
    uid = conn.execute("SELECT id FROM users WHERE line_user_id = 'demo:user:anong'").fetchone()[0]
    assert conn.execute("SELECT count(*) FROM messages WHERE user_id = %s", (uid,)).fetchone()[0] >= 6

    # Signing in again with the same (now empty) chat session changes nothing.
    assert _sign_in(client, "line", "demo:user:anong", chat_session=sid)["merged_chat"] is False


def test_same_place_label_keeps_one_place(seeded):
    client, _ = seeded
    first = _sign_in(client, "line", "demo:user:wichai")
    _say(client, first["session_token"], "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม")
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "บ้านแม่อยู่เสนา อยุธยา น้ำจะถึงไหม"})
    d = _sign_in(client, "line", "demo:user:wichai", chat_session=sid)
    [place] = _profile(client, d["session_token"])["places"]
    assert place["label"] == "บ้านแม่" and "เสนา" in place["where"]     # the newer, anonymous chat's place wins


def test_google_sign_in_creates_an_account_without_line(seeded):
    client, conn = seeded
    d = _sign_in(client, "google", "demo:google:pim")
    assert d["user"]["identities"] == {"line": False, "google": "pim@demo.namma"}
    _say(client, d["session_token"], "บ้านอยู่บางบาล อยุธยา น้ำจะถึงไหม")
    again = _sign_in(client, "google", "demo:google:pim")
    assert [p["label"] for p in _profile(client, again["session_token"])["places"]] == ["บ้าน"]
    assert _profile(client, again["session_token"])["line_linked"] is False
    assert conn.execute("SELECT count(*) FROM users WHERE line_user_id IS NULL").fetchone()[0] == 1


def test_link_line_to_a_google_account_merges_the_line_chat_history(seeded):
    client, conn = seeded
    # Someone who has been chatting on the LINE OA (their LINE account already exists, with a saved place)…
    line = _sign_in(client, "line", "demo:user:dao")
    _say(client, line["session_token"], "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม")
    # …signs in on the web with Google, then links LINE: one account, with the LINE side's memory.
    g = _sign_in(client, "google", "demo:google:dao")
    r = client.post("/api/auth/link/line", json={"id_token": "demo:user:dao"}, headers=_auth(g["session_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["identities"] == {"line": True, "google": "dao@demo.namma"}
    assert [p["label"] for p in _profile(client, g["session_token"])["places"]] == ["บ้านแม่"]
    assert conn.execute("SELECT count(*) FROM users WHERE line_user_id = 'demo:user:dao'").fetchone()[0] == 1
    # Either sign-in now opens the same account.
    via_line = _sign_in(client, "line", "demo:user:dao")
    assert via_line["user"]["identities"]["google"] == "dao@demo.namma"


def test_link_google_to_a_line_account(seeded):
    client, _ = seeded
    line = _sign_in(client, "line", "demo:user:korn")
    r = client.post("/api/auth/link/google", json={"id_token": "demo:google:korn"}, headers=_auth(line["session_token"]))
    assert r.status_code == 200 and r.json()["data"]["identities"] == {"line": True, "google": "korn@demo.namma"}
    assert _sign_in(client, "google", "demo:google:korn")["user"]["identities"]["line"] is True


def test_link_conflicts_and_unlink_rules(seeded):
    client, _ = seeded
    a = _sign_in(client, "line", "demo:user:nok")
    t = a["session_token"]
    client.post("/api/auth/link/google", json={"id_token": "demo:google:nok"}, headers=_auth(t))
    # A second, different Google account on the same NamMa account is refused.
    r = client.post("/api/auth/link/google", json={"id_token": "demo:google:other"}, headers=_auth(t))
    assert r.status_code == 409
    r = client.post("/api/auth/link/line", json={"id_token": "demo:user:someone"}, headers=_auth(t))
    assert r.status_code == 409
    assert client.post("/api/auth/link/fax", json={"id_token": "x"}, headers=_auth(t)).status_code == 404

    # Unlink one; the last way to sign in can't be removed.
    r = client.delete("/api/auth/link/google", headers=_auth(t))
    assert r.status_code == 200 and r.json()["data"]["identities"] == {"line": True, "google": None}
    assert client.delete("/api/auth/link/line", headers=_auth(t)).status_code == 409


def test_logout_ends_the_session(seeded):
    client, _ = seeded
    t = _sign_in(client, "line", "demo:user:ploy")["session_token"]
    assert client.post("/api/auth/logout", headers=_auth(t)).status_code == 200
    assert client.get("/api/me", headers=_auth(t)).status_code == 401


def test_delete_my_account_erases_the_person(seeded):
    client, conn = seeded
    d = _sign_in(client, "line", "demo:user:lek")
    t = d["session_token"]
    client.post("/api/auth/link/google", json={"id_token": "demo:google:lek"}, headers=_auth(t))
    _say(client, t, "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม")
    _say(client, t, "น้ำเข้าบ้านแล้ว ช่วยด้วย")
    uid = conn.execute("SELECT id FROM users WHERE line_user_id = 'demo:user:lek'").fetchone()[0]

    r = client.delete("/api/me", headers=_auth(t))
    assert r.status_code == 200 and r.json()["data"] == {"deleted": True}
    assert client.get("/api/me", headers=_auth(t)).status_code == 401
    assert conn.execute("SELECT count(*) FROM users WHERE line_user_id = 'demo:user:lek'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM user_identities WHERE subject = 'demo:google:lek'").fetchone()[0] == 0
    for table in ("messages", "user_locations", "user_memory", "sessions"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s", (uid,)).fetchone()[0] == 0, table
    # Signing in again starts a fresh, empty account.
    fresh = _sign_in(client, "line", "demo:user:lek")
    assert _profile(client, fresh["session_token"])["places"] == []


def test_malformed_demo_tokens_are_rejected(seeded):
    client, _ = seeded
    for token in ("demo:admin", "demo:responder", "demo:user:Bad Name", "demo:google", "demo:google:x:y"):
        provider = "google" if "google" in token else "line"
        r = client.post(f"/api/auth/{provider}", json={"id_token": token})
        assert r.status_code == 401, token
