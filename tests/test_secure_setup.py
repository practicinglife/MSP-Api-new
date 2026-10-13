"""Get started → Secure it: certificate (upload / Let's Encrypt checks), sign-in, people, and the wizard order."""

import base64
import datetime as dt
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from tests.conftest import make_session


@pytest.fixture
def admin(app_client, db_factory, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from app import runtime

    monkeypatch.setenv("MSP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "_started_with", {k for k, v in os.environ.items() if v.strip()})
    monkeypatch.setattr(runtime, "_applied", {})
    app_client.cookies.set("msp_session", make_session(db_factory, ["Admin"], "admin@example.com"))
    return app_client


def _cert(name="tools.example.test", days=90):  # type: ignore[no-untyped-def]
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return cert, key, pem, key_pem


def test_wizard_asks_to_secure_before_connecting(admin):  # type: ignore[no-untyped-def]
    assert admin.get("/api/admin/getting-started").json()["step"] == "secure"
    assert admin.post("/api/admin/secure/step", json={"mode": "local"}).status_code == 200
    assert admin.get("/api/admin/getting-started").json()["step"] != "secure"


def test_status_lists_the_exact_access_table(admin):  # type: ignore[no-untyped-def]
    from app.auth.principal import ACCESS

    s = admin.get("/api/admin/secure").json()
    assert s["access"] == ACCESS and s["sso"]["app_roles"] == ["Reader", "Editor", "Approver", "Admin"]
    assert s["sso"]["redirect_uri"].endswith("/auth/callback")


def test_secure_setup_is_admin_only(app_client, db_factory):  # type: ignore[no-untyped-def]
    app_client.cookies.set("msp_session", make_session(db_factory, ["Approver"], "ap@example.com"))
    assert app_client.get("/api/admin/secure").status_code == 403
    assert app_client.post("/api/admin/users", json={"username": "x", "password": "p" * 12}).status_code == 403


def test_upload_pem_installs_and_moves_the_address(admin, tmp_path):  # type: ignore[no-untyped-def]
    from app import runtime

    _, _, pem, key_pem = _cert()
    r = admin.post("/api/admin/secure/tls/upload", json={"cert_pem": pem, "key_pem": key_pem})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["domain"] == "tools.example.test" and body["restart"] == "manual"  # not under `app.cli run` here
    settings = runtime.load_settings()
    assert settings["tls"]["source"] == "uploaded" and settings["external_url"] == "https://tools.example.test"
    assert settings["listen"] == "0.0.0.0" and settings["secure_step"] == "secure"
    assert (tmp_path / "tls" / "server.crt").exists()
    status = admin.get("/api/admin/secure").json()["tls"]
    assert status["source"] == "uploaded" and status["names"] == ["tools.example.test"]


def test_upload_pfx_and_the_checks(admin):  # type: ignore[no-untyped-def]
    cert, key, pem, key_pem = _cert()
    pfx = pkcs12.serialize_key_and_certificates(b"t", key, cert, None,
                                                serialization.BestAvailableEncryption(b"pw"))
    ok = admin.post("/api/admin/secure/tls/upload", json={"pfx_b64": base64.b64encode(pfx).decode(), "pfx_password": "pw"})
    assert ok.status_code == 200, ok.text
    wrong_pw = admin.post("/api/admin/secure/tls/upload", json={"pfx_b64": base64.b64encode(pfx).decode(), "pfx_password": "x"})
    assert wrong_pw.status_code == 400
    _, _, _, other_key = _cert()
    mismatch = admin.post("/api/admin/secure/tls/upload", json={"cert_pem": pem, "key_pem": other_key})
    assert mismatch.status_code == 400 and "doesn't belong" in mismatch.json()["detail"]
    wrong_name = admin.post("/api/admin/secure/tls/upload", json={"cert_pem": pem, "key_pem": key_pem, "domain": "other.example.test"})
    assert wrong_name.status_code == 400
    _, _, old_pem, old_key = _cert(days=-1)
    assert admin.post("/api/admin/secure/tls/upload", json={"cert_pem": old_pem, "key_pem": old_key}).status_code == 400


def test_lets_encrypt_input_checks(admin):  # type: ignore[no-untyped-def]
    r = admin.post("/api/admin/secure/tls/acme/start", json={"domain": "tools.yourcompany.com"})
    assert r.status_code == 400 and "Subscriber Agreement" in r.json()["detail"]
    r = admin.post("/api/admin/secure/tls/acme/start", json={"domain": "server.local", "agree": True})
    assert r.status_code == 400 and "public domain" in r.json()["detail"]
    r = admin.post("/api/admin/secure/tls/acme/check", json={"domain": "tools.yourcompany.com"})
    assert r.status_code == 400 and "Start again" in r.json()["detail"]


def test_lets_encrypt_flow_with_a_fake_acme(admin, monkeypatch):  # type: ignore[no-untyped-def]
    """start → TXT record → check (DNS-over-HTTPS) → finish installs the certificate."""
    import respx

    from app.services import tls_setup

    _, _, pem, key_pem = _cert("tools.yourcompany.com")

    def fake_start(domain, email, environment, listen):  # type: ignore[no-untyped-def]
        tls_setup._pending[domain] = tls_setup._Pending(domain=domain, environment=environment,
                                                        record_name=f"_acme-challenge.{domain}", record_value="abc123",
                                                        key_pem=key_pem.encode(), listen_on_network=listen)
        return {"domain": domain, "record_type": "TXT", "record_name": f"_acme-challenge.{domain}",
                "record_value": "abc123", "environment": environment}

    monkeypatch.setattr(tls_setup, "_start_sync", fake_start)
    monkeypatch.setattr(tls_setup, "_finish_sync", lambda pending: pem.encode())
    r = admin.post("/api/admin/secure/tls/acme/start", json={"domain": "tools.yourcompany.com", "agree": True}).json()
    assert r["record_name"] == "_acme-challenge.tools.yourcompany.com" and r["record_value"] == "abc123"
    with respx.mock() as mock:
        mock.get(url__startswith="https://cloudflare-dns.com/").respond(200, json={"Answer": [{"data": '"abc123"'}]})
        mock.get(url__startswith="https://dns.google/").respond(200, json={})
        check = admin.post("/api/admin/secure/tls/acme/check", json={"domain": "tools.yourcompany.com"}).json()
    assert check["ready"] is True
    done = admin.post("/api/admin/secure/tls/acme/finish", json={"domain": "tools.yourcompany.com"}).json()
    assert done["source"] == "letsencrypt" and done["address"] == "https://tools.yourcompany.com"


def test_sso_save_applies_now_and_turns_off(admin, monkeypatch):  # type: ignore[no-untyped-def]
    from app.api.auth_m365 import oidc_configured

    for var in ("MSP_ENTRA_TENANT_ID", "MSP_ENTRA_CLIENT_ID", "MSP_ENTRA_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert admin.post("/api/admin/secure/sso", json={"provider": "entra", "tenant_id": "t"}).status_code == 400
    r = admin.post("/api/admin/secure/sso", json={"provider": "entra", "tenant_id": "tid", "client_id": "cid",
                                                  "client_secret": "SECRET-1"})
    assert r.status_code == 200 and r.json()["active"], r.text
    assert oidc_configured() and os.environ["MSP_ENTRA_CLIENT_SECRET"] == "SECRET-1"
    from app import runtime

    assert "SECRET-1" not in str(runtime.load_settings())  # stored encrypted
    assert admin.post("/api/admin/secure/sso", json={"provider": "none"}).json() == {"active": False}
    assert not oidc_configured()


def test_sso_in_env_wins(admin, monkeypatch):  # type: ignore[no-untyped-def]
    from app import runtime

    monkeypatch.setattr(runtime, "_started_with", {"MSP_ENTRA_TENANT_ID"})
    assert admin.post("/api/admin/secure/sso", json={"provider": "none"}).status_code == 409


def test_people_with_one_role_each(admin):  # type: ignore[no-untyped-def]
    r = admin.post("/api/admin/users", json={"username": "Pat", "password": "a-long-password-1", "role": "approver"})
    assert r.status_code == 200 and r.json()["role"] == "Approver" and r.json()["totp_qr"].startswith("data:image/svg")
    assert admin.post("/api/admin/users", json={"username": "pat", "password": "a-long-password-1"}).status_code == 409
    assert admin.post("/api/admin/users", json={"username": "x y", "password": "a-long-password-1"}).status_code == 400
    assert admin.patch("/api/admin/users/pat", json={"role": "reader"}).json()["role"] == "Reader"
    users = {u["username"]: u for u in admin.get("/api/admin/secure").json()["users"]}
    assert users["pat"]["role"] == "Reader"


def test_apply_settings_never_overrides_env(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    from app import runtime

    monkeypatch.setenv("MSP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MSP_EXTERNAL_BASE_URL", "https://from-env.example")
    monkeypatch.setattr(runtime, "_started_with", {"MSP_EXTERNAL_BASE_URL"})
    monkeypatch.setattr(runtime, "_applied", {})
    runtime.save_settings(external_url="https://from-wizard.example")
    runtime.apply_settings()
    assert os.environ["MSP_EXTERNAL_BASE_URL"] == "https://from-env.example"

    monkeypatch.setattr(runtime, "_started_with", set())
    monkeypatch.setenv("MSP_EXTERNAL_BASE_URL", "https://localhost")  # the local runner's default
    runtime.save_settings(external_url="")
    runtime.apply_settings()
    assert os.environ["MSP_EXTERNAL_BASE_URL"] == "https://localhost"  # not something the wizard set: left alone


def test_last_local_admin_is_kept(admin):  # type: ignore[no-untyped-def]
    admin.post("/api/admin/users", json={"username": "boss", "password": "a-long-password-1", "role": "admin"})
    r = admin.patch("/api/admin/users/boss", json={"role": "reader"})
    assert r.status_code == 409 and "break-glass" in r.json()["detail"]
    assert admin.patch("/api/admin/users/boss", json={"disabled": True}).status_code == 409
