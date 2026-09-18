from noc_agents.adapters.email_smtp import parse_subject_body, send_email, email_configured


def test_parse_subject_body():
    sub, body = parse_subject_body("Subject: Hello INC\n\nLine one\nLine two")
    assert sub == "Hello INC"
    assert "Line one" in body


def test_send_email_mocks_without_credentials(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_EMAIL_TO", raising=False)
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    assert email_configured() is False
    r = send_email(subject="t", body="b")
    assert r.ok is True
    assert r.mode == "mock"


def test_send_email_mock_with_to_but_no_password(monkeypatch):
    monkeypatch.setenv("DEMO_EMAIL_TO", "demo@example.com")
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    r = send_email(subject="t", body="b")
    assert r.ok is True
    assert r.mode == "mock"
    assert "demo@example.com" in r.to
