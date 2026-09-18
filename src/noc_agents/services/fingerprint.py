from __future__ import annotations


def build_fingerprint(site_id: str, alarm_code: str, failure_domain: str) -> str:
    return f"{site_id.strip().upper()}|{alarm_code.strip().upper()}|{failure_domain.strip().upper()}"
