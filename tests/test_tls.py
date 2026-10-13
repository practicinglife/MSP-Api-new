"""Local HTTPS certificates (app/tls.py, ``python -m app.cli make-certs``)."""

import ipaddress
import ssl

from cryptography import x509

from app.cli import main
from app.tls import make_certs


def _sans(path):
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(ext.get_values_for_type(x509.DNSName)), set(ext.get_values_for_type(x509.IPAddress))


def test_creates_ca_and_server_cert_for_localhost(tmp_path):
    paths = make_certs(tmp_path)
    assert paths.ca_created
    dns, ips = _sans(paths.server_cert)
    assert dns == {"localhost"}
    assert ips == {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
    # The server cert verifies against the CA and the pair loads into an SSL context.
    ctx = ssl.create_default_context(cafile=str(paths.ca_cert))
    ctx.load_cert_chain(paths.server_cert, paths.server_key)
    ca = x509.load_pem_x509_certificate(paths.ca_cert.read_bytes())
    leaf = x509.load_pem_x509_certificate(paths.server_cert.read_bytes())
    leaf.verify_directly_issued_by(ca)


def test_rerun_keeps_ca_and_adds_names(tmp_path):
    first = make_certs(tmp_path)
    ca_before = first.ca_cert.read_bytes()
    second = make_certs(tmp_path, ["devbox.test", "192.168.1.50"])
    assert not second.ca_created
    assert second.ca_cert.read_bytes() == ca_before
    dns, ips = _sans(second.server_cert)
    assert "devbox.test" in dns
    assert ipaddress.ip_address("192.168.1.50") in ips


def test_cli_make_certs(tmp_path, capsys):
    main(["make-certs", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "certutil -user -addstore Root" in out
    assert (tmp_path / "server.crt").exists() and (tmp_path / "server.key").exists()
