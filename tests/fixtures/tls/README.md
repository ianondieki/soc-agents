# TLS test fixtures — TEST ONLY, never trust these anywhere else

`test_only_cert.pem` and `test_only_key.pem` are a **self-signed certificate and its private key,
generated for the unit tests and committed on purpose.** The key is public by being here. It
protects nothing and must never be installed, trusted or reused outside this test suite.

What they are for: `tests/unit/test_kmd_cap.py` and `tests/unit/test_flood.py` run a real TLS
server on the **loopback interface (127.0.0.1) only**, to prove that the total fetch deadline
(`adapters/kmd_cap.py` `DeadlineWatchdog`) holds over HTTPS — review finding W02-TLS. The bug
lived below httpx, in the socket that TLS wraps, so neither MockTransport nor a plain-HTTP
server could exhibit it; both production feeds are `https`.

How the tests trust it: each test builds its **own** `httpx.Client` with
`verify=ssl.create_default_context(cafile=<this cert>)`, so certificate and hostname
verification stay fully on (Python 3.13's default strict X.509 mode included). No production
code disables verification, and nothing here is read by production code.

How it was made (no network; OpenSSL 3.5.6 as shipped with Git for Windows):

```
MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 36500 \
  -keyout test_only_key.pem -out test_only_cert.pem \
  -subj "/O=noc-agents TEST ONLY - loopback tests, never trust/CN=127.0.0.1" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost"
```

Valid until 2126 so the suite does not start failing on a date. Why a static file rather than
generating one per run: the `cryptography` and `trustme` packages are not installed and this lane
may not add a dependency.
