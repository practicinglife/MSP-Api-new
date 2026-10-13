"""The four roles do exactly what the access list says (app/auth/principal.py ACCESS) — every row, every role."""

import httpx
import pytest
import respx

from app.auth.principal import (
    ACCESS,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_EDITOR,
    ROLE_READER,
    RANK,
    Principal,
    normalize_roles,
)
from tests.conftest import make_session
from tests.test_connections import FIX

ROLES = [ROLE_READER, ROLE_EDITOR, ROLE_APPROVER, ROLE_ADMIN]
H = {"X-CSRF-Token": "csrf-token"}


@pytest.fixture
def world(app_client, db_factory):  # type: ignore[no-untyped-def]
    """A petstore connection in direct write mode, and a way to sign in as any role."""
    def as_role(role: str):  # type: ignore[no-untyped-def]
        app_client.cookies.set("msp_session", make_session(db_factory, [role], f"{role.lower()}@example.com"))
        return app_client

    admin = as_role(ROLE_ADMIN)
    r = admin.post("/api/connections", json={"kind": "openapi", "name": "Petstore",
                                             "source": {"text": (FIX / "petstore.yaml").read_text(encoding="utf-8")}})
    assert r.status_code == 200, r.text
    assert admin.put("/api/admin/write-modes/petstore", json={"write_mode": "direct"}).status_code == 200
    return as_role


def _allowed(status: int) -> bool:
    return status not in (401, 403)


def check(row: str, as_role) -> dict[str, bool]:  # type: ignore[no-untyped-def]
    """Can each role do what ``row`` describes? One or more real requests per row."""
    out = {}
    for role in ROLES:
        c = as_role(role)
        if row.startswith("Sign in, search"):
            ok = _allowed(c.get("/api/connections").status_code) and \
                _allowed(c.get("/api/connections/petstore/operations").status_code)
        elif row.startswith("Call read-only"):
            with respx.mock(assert_all_called=False) as mock:
                mock.get("https://api.petstore.example.com/v1/pets/1").mock(return_value=httpx.Response(200, json={}))
                ok = _allowed(c.post("/api/connections/petstore/operations/show_pet_by_id/call",
                                     json={"args": {"petId": 1}}).status_code)
        elif row.startswith("Propose a write"):
            ok = _allowed(c.post("/api/changes", json={"command": "no.such.command", "params": {}}).status_code)
        elif row.startswith("Call write"):
            with respx.mock(assert_all_called=False) as mock:
                mock.post("https://api.petstore.example.com/v1/pets").mock(return_value=httpx.Response(201, json={}))
                ok = _allowed(c.post("/api/connections/petstore/operations/create_pet/call",
                                     json={"args": {"body": {"name": "x"}}}).status_code)
        elif row.startswith("Submit change requests"):
            ok = _allowed(c.post("/api/changes/nope/submit", json={"confirmation": ""}, headers=H).status_code)
        elif row.startswith("Give final approval"):
            ok = _allowed(c.post("/api/changes/nope/approve", headers=H).status_code)
        elif row.startswith("Add, edit and delete"):
            ok = _allowed(c.post("/api/connections", json={"kind": "openapi", "source": {"text": "x"}}).status_code) \
                and _allowed(c.patch("/api/connections/nope", json={"enabled": True}).status_code) \
                and _allowed(c.post("/api/connectors/register", json={"platform": "sentinelone",
                                                                      "credentials": {}}).status_code)
        elif row.startswith("Set write modes"):
            ok = _allowed(c.put("/api/admin/write-modes/petstore", json={"write_mode": "direct"}).status_code) and \
                _allowed(c.get("/api/admin/connections/export").status_code)
        elif row.startswith("Users, connection keys"):
            ok = all(_allowed(c.get(u).status_code) for u in
                     ("/api/admin/api-keys", "/api/admin/setup", "/api/admin/audit", "/api/admin/getting-started"))
        else:
            raise AssertionError(f"No check for access row: {row}")
        out[role] = ok
    return out


@pytest.mark.parametrize("row", ACCESS, ids=lambda r: r["what"][:40])
def test_access_list_is_exactly_what_is_enforced(world, row):  # type: ignore[no-untyped-def]
    got = check(row["what"], world)
    expected = {role: RANK[role] >= RANK[row["role"]] for role in ROLES}
    assert got == expected


def test_old_role_names_still_work():
    assert normalize_roles(["MSPTools.User"]) == {ROLE_READER}
    assert normalize_roles(["MSPTools.Editor", "x"]) == {ROLE_EDITOR}
    assert normalize_roles(["MSPTools.Approver"]) == {ROLE_APPROVER}
    p = Principal(id="x", kind="session", roles=frozenset({"MSPTools.Admin"}))
    assert p.roles == {ROLE_ADMIN} and p.has_role(ROLE_READER) and p.can_edit


def test_editor_submits_approver_approves(world):  # type: ignore[no-untyped-def]
    """Two people for an approval-mode write: an Editor can submit but not approve."""
    admin = world(ROLE_ADMIN)
    assert admin.put("/api/admin/write-modes/petstore", json={"write_mode": "approval"}).status_code == 200
    editor = world(ROLE_EDITOR)
    proposed = editor.post("/api/connections/petstore/operations/create_pet/call", json={"args": {"body": {"name": "x"}}})
    assert proposed.status_code == 202
    change = proposed.json()["change"]["id"]
    assert editor.post(f"/api/changes/{change}/submit", json={"confirmation": ""}, headers=H).status_code == 200
    assert editor.post(f"/api/changes/{change}/approve", headers=H).status_code == 403
    approver = world(ROLE_APPROVER)
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.petstore.example.com/v1/pets").mock(return_value=httpx.Response(201, json={}))
        assert approver.post(f"/api/changes/{change}/approve", headers=H).status_code == 200


def test_keys_are_reader_or_editor_never_approver_or_admin(world):  # type: ignore[no-untyped-def]
    admin = world(ROLE_ADMIN)
    assert admin.post("/api/admin/api-keys", json={"name": "k", "role": "admin"}).status_code == 400
    assert admin.post("/api/admin/api-keys", json={"name": "k", "role": "approver"}).status_code == 400
    assert admin.post("/api/admin/api-keys", json={"name": "k", "role": "reader", "scopes": ["*:write"]}).status_code == 400
    editor_key = admin.post("/api/admin/api-keys", json={"name": "k", "role": "editor", "scopes": ["*:write"]}).json()
    assert editor_key["roles"] == [ROLE_EDITOR]
    legacy = admin.post("/api/admin/api-keys", json={"name": "k2", "role": "user"}).json()
    assert legacy["roles"] == [ROLE_READER]
    admin.cookies.clear()
    h = {"X-API-Key": editor_key["key"]}
    assert admin.get("/api/admin/api-keys", headers=h).status_code == 403  # never reaches Admin
    assert admin.put("/api/admin/write-modes/petstore", json={"write_mode": "approval"}, headers=h).status_code == 403
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://api.petstore.example.com/v1/pets").mock(return_value=httpx.Response(201, json={}))
        r = admin.post("/api/connections/petstore/operations/create_pet/call", json={"args": {"body": {"name": "x"}}},
                       headers=h)
    assert r.status_code == 200, r.text  # an Editor key writes where the write mode is direct


def test_read_only_write_mode_stops_everyone(world):  # type: ignore[no-untyped-def]
    admin = world(ROLE_ADMIN)
    assert admin.put("/api/admin/write-modes/petstore", json={"write_mode": "read_only"}).status_code == 200
    r = admin.post("/api/connections/petstore/operations/create_pet/call", json={"args": {"body": {"name": "x"}}})
    assert r.status_code == 403 and "read-only" in r.text
