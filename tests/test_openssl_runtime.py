"""Offline TLS regressions; the script entrypoint also checks image packages."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def verify_image_packages():
    for package in ("libssl3t64", "openssl", "openssl-provider-legacy"):
        version = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", package], text=True
        )
        if version != "3.5.7-1~deb13u3":
            raise AssertionError(f"Unexpected {package} version: {version}")
        print(f"{package}: {version}", flush=True)
    cli_version = subprocess.check_output(["openssl", "version"], text=True).strip()
    # A Debian backport package version need not equal the library version string.
    cli_version = cli_version.split(" (Library:", 1)[0]
    if ssl.OPENSSL_VERSION != cli_version:
        raise AssertionError(f"Python/CLI SSL mismatch: {ssl.OPENSSL_VERSION} != {cli_version}")
    print(f"Python SSL: {ssl.OPENSSL_VERSION}", flush=True)


class OpenSSLRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tls.example.com")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(minutes=5))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("tls.example.com")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        self.cert_path = Path(self.temp.name) / "certificate.pem"
        key_path = Path(self.temp.name) / "private.key"
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        self.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_context.load_cert_chain(self.cert_path, key_path)

    def peers(self, version, trust=True):
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if trust:
            client_context.load_verify_locations(self.cert_path)
        client_context.minimum_version = client_context.maximum_version = version
        self.server_context.minimum_version = self.server_context.maximum_version = version
        client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
        client = client_context.wrap_bio(client_in, client_out, server_hostname="tls.example.com")
        server = self.server_context.wrap_bio(server_in, server_out, server_side=True)
        return client, server, client_in, client_out, server_in, server_out

    def handshake(self, peers):
        client, server, client_in, client_out, server_in, server_out = peers
        complete = set()
        for _ in range(30):
            for peer in (client, server):
                if peer in complete:
                    continue
                try:
                    peer.do_handshake()
                    complete.add(peer)
                except ssl.SSLWantReadError:
                    pass
            for source, destination in ((client_out, server_in), (server_out, client_in)):
                data = source.read()
                if data:
                    destination.write(data)
            if len(complete) == 2:
                return
        self.fail("TLS handshake did not finish within the bounded exchange")

    def test_tls12_and_tls13_trusted_roundtrip(self):
        for version, expected in ((ssl.TLSVersion.TLSv1_2, "TLSv1.2"), (ssl.TLSVersion.TLSv1_3, "TLSv1.3")):
            with self.subTest(version=expected):
                peers = self.peers(version)
                self.handshake(peers)
                client, server, client_in, client_out, server_in, server_out = peers
                self.assertEqual(client.version(), expected)
                self.assertEqual(server.version(), expected)
                request = b"synthetic TLS request"
                client.write(request)
                ciphertext = client_out.read()
                self.assertNotIn(request, ciphertext)
                server_in.write(ciphertext)
                self.assertEqual(server.read(), request)
                server.write(b"synthetic TLS response")
                client_in.write(server_out.read())
                self.assertEqual(client.read(), b"synthetic TLS response")

    def test_untrusted_certificate_is_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake(self.peers(ssl.TLSVersion.TLSv1_3, trust=False))


if __name__ == "__main__":
    verify_image_packages()
    unittest.main(verbosity=2)
