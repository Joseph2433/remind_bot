from lack_bot.redaction import redact_text


def test_redacts_common_key_value_secrets():
    text = "password=hunter2 token: abc123 secret = very-secret api_key = key-123"

    redacted = redact_text(text)

    assert "hunter2" not in redacted
    assert "abc123" not in redacted
    assert "very-secret" not in redacted
    assert "key-123" not in redacted
    assert redacted.count("[REDACTED]") == 4


def test_redacts_authorization_bearer_tokens():
    redacted = redact_text("Authorization: Bearer lark-token-value")

    assert "lark-token-value" not in redacted
    assert "Bearer [REDACTED]" in redacted
