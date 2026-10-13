"""``python -m app.cli run``: start the app on this computer from a fresh checkout — no Docker, no .env needed.

Anything already set (in ``.env`` or the environment) wins. What isn't set gets a local default:

* database — SQLite in ``data/api-tools.db`` (PostgreSQL via ``DATABASE_URL`` for real use);
* encryption key — generated once into ``data/master.key`` (back it up with the database; keep it secret);
* HTTPS — a certificate for localhost from a local CA in ``certs/`` (``python -m app.cli make-certs``);
* packs — whatever the Get started wizard chose (saved in ``data/settings.json``); ``--packs`` or
  ``MSP_PACKS`` override it;
* ``MSP_EXTERNAL_BASE_URL`` — ``https://localhost`` (with the port when it isn't 443).

Then it prints where to go and the first-run setup code, and serves the app.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

DEFAULT_DATA = Path("data")


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _database_reachable(url: str) -> bool:
    """For a network database (PostgreSQL …): is anything listening at its host and port?"""
    from sqlalchemy.engine import make_url

    try:
        u = make_url(url)
    except Exception:  # noqa: BLE001
        return True
    if u.get_backend_name() == "sqlite" or not u.host:
        return True
    try:
        with socket.create_connection((u.host, u.port or 5432), timeout=2):
            return True
    except OSError:
        return False


def prepare(data_dir: Path = DEFAULT_DATA, port: int = 443, packs: str | None = None, *,
            sqlite: bool = False, no_sso: bool = False) -> dict[str, str]:
    """Fill in local defaults for whatever isn't configured. Returns what was decided (for the banner).

    ``sqlite`` uses the local SQLite file even when ``.env`` names another database; ``no_sso`` turns single
    sign-on off for this run (local accounts only), e.g. before the redirect URI is registered."""
    from app import runtime
    from app.auth.crypto import generate_master_key
    from app.tls import make_certs

    runtime.set_by_operator("")  # remember what .env / the environment set, before adding defaults
    data_dir.mkdir(parents=True, exist_ok=True)
    notes: dict[str, str] = {}

    if not os.environ.get("MSP_MASTER_KEY", "").strip():
        key_file = data_dir / "master.key"
        if not key_file.exists():
            key_file.write_text(generate_master_key() + "\n", encoding="utf-8")
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
            notes["key"] = f"created {key_file}"
        else:
            notes["key"] = f"from {key_file}"
        os.environ["MSP_MASTER_KEY"] = key_file.read_text(encoding="utf-8").strip()
    else:
        notes["key"] = "from .env"

    if sqlite:
        os.environ.pop("DATABASE_URL", None)
    if not os.environ.get("DATABASE_URL", "").strip():
        db = (data_dir / "api-tools.db").resolve()
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db.as_posix()}"
        notes["database"] = f"SQLite: {db}"
    else:
        notes["database"] = "DATABASE_URL from .env"
        if not _database_reachable(os.environ["DATABASE_URL"]):
            sys.exit("The database in DATABASE_URL isn't reachable (is PostgreSQL / docker compose running?). "
                     "Start it, or run with --sqlite to use the local database in data/ instead.")

    if no_sso:
        for var in ("MSP_ENTRA_TENANT_ID", "MSP_ENTRA_CLIENT_ID", "MSP_ENTRA_CLIENT_SECRET", "MSP_OIDC_ISSUER"):
            os.environ[var] = ""
        os.environ["MSP_NO_SSO"] = "1"
        notes["sso"] = "off for this run (--no-sso): local accounts only"
    else:
        sso = any(os.environ.get(v, "").strip() for v in ("MSP_ENTRA_TENANT_ID", "MSP_OIDC_ISSUER"))
        notes["sso"] = "on (from .env)" if sso else "off: local accounts"

    os.environ["MSP_DATA_DIR"] = str(data_dir)  # settings.json: choices made in the Get started wizard
    if packs is not None:
        os.environ["MSP_PACKS"] = "" if packs == "none" else packs
    notes["packs"] = _packs_note()

    if runtime.load_settings().get("port") != port:
        runtime.save_settings(port=port)  # so an installed certificate's address includes a non-443 port
    runtime.apply_settings()  # the address and sign-in chosen in Get started (where .env didn't set them)
    base = "https://localhost" + ("" if port == 443 else f":{port}")
    os.environ.setdefault("MSP_EXTERNAL_BASE_URL", base)
    notes["url"] = os.environ["MSP_EXTERNAL_BASE_URL"]
    notes.update(_certificate(make_certs))
    return notes


def _certificate(make_certs) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """The certificate to serve: one installed in Get started (Let's Encrypt / uploaded), else the local one."""
    from app import runtime

    tls = runtime.load_settings().get("tls") or {}
    if tls.get("cert") and Path(tls["cert"]).exists() and Path(tls.get("key", "")).exists():
        return {"certs": f"{tls.get('source', 'installed')} certificate for {tls.get('domain', '?')}",
                "cert": tls["cert"], "cert_key": tls["key"], "ca": ""}
    certs = Path("certs")
    note = f"from {certs}"
    if not (certs / "server.crt").exists() or not (certs / "server.key").exists():
        paths = make_certs(certs)
        note = f"created ({paths.ca_cert} is the local CA)"
    return {"certs": note, "cert": str(certs / "server.crt"), "cert_key": str(certs / "server.key"),
            "ca": str(certs / "ca.crt")}


def run(host: str | None = None, port: int = 443, data_dir: Path = DEFAULT_DATA, packs: str | None = None, *,
        sqlite: bool = False, no_sso: bool = False) -> None:
    """``host`` None: 127.0.0.1, or every network interface once Get started installed a real certificate and
    chose "accept connections from other computers"."""
    from app import runtime

    os.environ["MSP_DATA_DIR"] = str(data_dir)
    listen = host or runtime.load_settings().get("listen") or "127.0.0.1"
    if not _port_free(listen, port):
        fallback = 8443
        if port == 443 and _port_free(listen, fallback):
            print(f"Port 443 is busy or not allowed here; using {fallback}.")
            port = fallback
        else:
            sys.exit(f"Port {port} is in use. Pick another with --port.")
    notes = prepare(data_dir, port, packs, sqlite=sqlite, no_sso=no_sso)

    from app.api.setup import setup_code

    code = setup_code()
    # Plain ASCII: Windows consoles often can't print box-drawing characters.
    line = "-" * 72
    print()
    print(line)
    print(f"  API Tools  ->  {notes['url']}")
    print(line)
    print(f"  First run: open {notes['url']}/setup and enter setup code  {code}")
    print("             (needed only until the first account exists)")
    print(f"  Database:  {notes['database']}")
    print(f"  Key:       {notes['key']} - back it up with the database, keep it secret")
    print(f"  Packs:     {notes['packs']}")
    print(f"  Sign-in:   single sign-on {notes['sso']}")
    print(f"  HTTPS:     certificate {notes['certs']}")
    if notes["ca"]:
        print(f"             browsers warn until you trust the CA once:  certutil -user -addstore Root {notes['ca']}")
    print(f"  Listening: {listen}:{port}" + ("  (this computer only)" if listen.startswith("127.") else ""))
    print(line, flush=True)

    import asyncio

    import uvicorn

    from app.tls import make_certs

    # Serve; when Get started changes the packs, the certificate or the address, rebuild the app in this same
    # process (re-reading the saved settings) and serve again.
    while True:
        from app.main import create_app

        runtime.apply_settings()
        cert = _certificate(make_certs)
        listen = host or runtime.load_settings().get("listen") or "127.0.0.1"
        # timeout_graceful_shutdown: browsers keep idle connections open, which would hold a rebuild forever
        server = uvicorn.Server(uvicorn.Config(create_app(), host=listen, port=port, ssl_certfile=cert["cert"],
                                               ssl_keyfile=cert["cert_key"], timeout_graceful_shutdown=3))
        wanted = {"restart": False}

        def _restart(server: uvicorn.Server = server, wanted: dict[str, bool] = wanted) -> None:
            wanted["restart"] = True
            # Let the response that asked for it go out first, then stop serving.
            asyncio.get_running_loop().call_later(0.5, lambda: setattr(server, "should_exit", True))

        runtime.set_restart_handler(_restart)
        server.run()
        runtime.set_restart_handler(None)
        if not wanted["restart"]:
            break
        runtime.apply_settings()
        print(f"\nRestarting to apply the new setup (packs: {_packs_note()}; address: "
              f"{os.environ.get('MSP_EXTERNAL_BASE_URL', '')})...\n", flush=True)


def _packs_note() -> str:
    from app import packs

    names = packs.configured()
    source = ".env" if packs.from_env() else ("Get started" if packs.chosen() else "not chosen yet")
    return f"{', '.join(names) or 'none (core only)'}  ({source})"
