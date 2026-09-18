# Phase 0 step (e): can *Python* make an HTTPS call from this machine?
#
# Why this exists: Windows keeps its own certificate store ("Schannel"), which
# curl, Edge and PowerShell use. Python does NOT use it -- httpx/requests trust a
# bundle of certificates shipped inside the `certifi` package instead. On a
# corporate network that inspects TLS traffic, the proxy re-signs every
# connection with a company certificate that lives in the Windows store but not
# in certifi, so curl succeeds and Python fails with CERTIFICATE_VERIFY_FAILED.
# Finding that out now costs a minute; finding it out in Phase 3 costs a day.
#
# Usage (no arguments, no project install needed):
#     C:\Python313\python.exe scripts\check_tls.py
#
# Exit codes:  0 = PASS   1 = FAIL (certificate or HTTP error)   2 = no network

import os
import socket
import ssl
import sys

URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=-1.28&longitude=36.82&current=precipitation"  # Nairobi
)
TIMEOUT = 15.0

try:  # httpx is what the real adapters use, so prefer it
    import httpx
except ImportError:  # ...but this script must still answer before `pip install -e .`
    httpx = None
import urllib.error
import urllib.request


def ca_bundle() -> str:
    """Where Python looks for trusted certificate authorities."""
    try:
        import certifi

        return "certifi bundle: " + certifi.where()
    except ImportError:
        paths = ssl.get_default_verify_paths()
        return f"stdlib default: cafile={paths.cafile} capath={paths.capath}"


def probe() -> tuple[str, int]:
    """Fetch the URL. Returns (client name, HTTP status); raises on failure."""
    if httpx is not None:
        return "httpx " + httpx.__version__, httpx.get(URL, timeout=TIMEOUT).status_code
    with urllib.request.urlopen(URL, timeout=TIMEOUT) as resp:  # noqa: S310 - fixed https URL
        return "urllib.request (httpx not installed)", resp.status


def classify(exc: BaseException) -> str:
    """Name the failure mode. Libraries wrap the real cause, so walk the chain."""
    seen, cur = 0, exc
    while cur is not None and seen < 10:
        if isinstance(cur, ssl.SSLCertVerificationError):
            return "cert"
        if isinstance(cur, socket.gaierror):  # name lookup failed -> offline/DNS
            return "dns"
        if isinstance(cur, urllib.error.HTTPError):
            return "http"
        if isinstance(cur, (TimeoutError, socket.timeout)):
            return "timeout"
        name = type(cur).__name__
        if name in ("ConnectTimeout", "ReadTimeout", "PoolTimeout", "TimeoutException"):
            return "timeout"
        if name in ("ConnectError", "URLError", "OSError", "ProxyError"):
            pass  # too generic on its own -- keep unwrapping
        cur, seen = cur.__cause__ or cur.__context__, seen + 1
    return "other"


def cert_remedy() -> None:
    """The one fix that works behind a TLS-inspecting proxy."""
    print("  Fix:  C:\\Python313\\python.exe -m pip install truststore")
    print("        then set NOC_USE_TRUSTSTORE=1 (add it to .env)")
    print("  Why:  the adapters call truststore.inject_into_ssl() when that variable")
    print("        is set, which makes Python verify against the Windows certificate")
    print("        store (where your proxy's CA already is) instead of certifi.")
    print("  Note: a working `curl` proves NOTHING here -- curl uses Schannel,")
    print("        Python uses certifi. They are separate trust stores.")


def main() -> int:
    print("NOC TLS check -- Phase 0 step (e)")
    print(f"  Python:     {sys.version.split()[0]}  ({sys.executable})")
    print(f"  CA source:  {ca_bundle()}")
    print(f"  URL:        {URL}")
    flag = os.environ.get("NOC_USE_TRUSTSTORE", "")
    print(f"  NOC_USE_TRUSTSTORE={flag or '(unset)'}")
    print()

    try:
        client, status = probe()
    except Exception as exc:  # never dump a traceback at the operator
        mode = classify(exc)
        client = "httpx" if httpx is not None else "urllib.request"
        print(f"  Client:     {client}")

        if mode == "cert":
            print(f"  FAIL: certificate verification failed -- {exc}")
            print("  Meaning: something re-signed the connection (corporate TLS")
            print("           inspection, or an out-of-date CA bundle).")
            # The flag is already on, so try the documented fix once, here and now.
            if flag == "1":
                try:
                    import truststore

                    truststore.inject_into_ssl()
                    print("\n  NOC_USE_TRUSTSTORE=1: injected truststore, retrying once...")
                    client, status = probe()
                    print(f"  PASS after truststore injection (HTTP {status}, {client}).")
                    print("  Keep NOC_USE_TRUSTSTORE=1 set for every run.")
                    return 0
                except ImportError:
                    print("\n  NOC_USE_TRUSTSTORE=1 is set but truststore is NOT installed.")
                except Exception as retry_exc:
                    print(f"\n  Retry with truststore ALSO failed: {retry_exc}")
            cert_remedy()
            return 1

        if mode == "dns":
            print(f"  NO NETWORK: cannot resolve the hostname -- {exc}")
            print("  This is an offline/DNS problem, not a certificate problem.")
            print("  Reconnect (or check the proxy settings) and re-run.")
            return 2
        if mode == "timeout":
            print(f"  NO NETWORK: timed out after {TIMEOUT:.0f}s -- {exc}")
            print("  The host never answered: blocked port, dead proxy, or slow link.")
            return 2
        if mode == "http":
            print(f"  FAIL: TLS was fine, the server returned an error -- {exc}")
            return 1
        print(f"  FAIL: unexpected {type(exc).__name__}: {exc}")
        return 1

    print(f"  Client:     {client}")
    print(f"  HTTP:       {status}")
    if 200 <= status < 300:
        print("\n  PASS: Python can verify this TLS certificate. No proxy interception")
        print("        seen; truststore is not needed on this machine.")
        return 0
    print(f"\n  FAIL: TLS handshake succeeded but the API answered HTTP {status}.")
    print("        Certificates are fine; the endpoint or query string is not.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
