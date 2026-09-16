"""Tests for opløsning af hemmeligheder.

Dækker de tre måder en webhook kan sættes på, plus fejlhåndtering.
"""

import pytest

from auction_hunter.secrets import SecretError, get_secret, redact


def test_reads_plain_environment_variable(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    assert get_secret("DISCORD_WEBHOOK_URL") == "https://discord.com/api/webhooks/1/abc"


def test_file_takes_precedence_over_environment(tmp_path, monkeypatch):
    """Docker secrets-stilen skal vinde over en direkte variabel."""
    secret_file = tmp_path / "webhook"
    secret_file.write_text("https://fra-fil\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://fra-env")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_FILE", str(secret_file))
    assert get_secret("DISCORD_WEBHOOK_URL") == "https://fra-fil"


def test_indirect_lookup_via_from_env(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_FROM_ENV", "MIN_WEBHOOK")
    monkeypatch.setenv("MIN_WEBHOOK", "https://indirekte")
    assert get_secret("DISCORD_WEBHOOK_URL") == "https://indirekte"


def test_missing_indirect_target_is_an_error(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_FROM_ENV", "FINDES_IKKE")
    monkeypatch.delenv("FINDES_IKKE", raising=False)
    with pytest.raises(SecretError, match="FINDES_IKKE"):
        get_secret("DISCORD_WEBHOOK_URL")


def test_missing_file_is_an_error(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_FILE", "/findes/ikke")
    with pytest.raises(SecretError, match="Kunne ikke læse"):
        get_secret("DISCORD_WEBHOOK_URL")


def test_empty_file_is_an_error(tmp_path, monkeypatch):
    secret_file = tmp_path / "tom"
    secret_file.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_FILE", str(secret_file))
    with pytest.raises(SecretError, match="tom"):
        get_secret("DISCORD_WEBHOOK_URL")


def test_returns_none_when_optional_and_absent(monkeypatch):
    for suffix in ("", "_FILE", "_FROM_ENV"):
        monkeypatch.delenv(f"DISCORD_WEBHOOK_URL{suffix}", raising=False)
    assert get_secret("DISCORD_WEBHOOK_URL") is None


def test_required_raises_with_guidance(monkeypatch):
    for suffix in ("", "_FILE", "_FROM_ENV"):
        monkeypatch.delenv(f"DISCORD_WEBHOOK_URL{suffix}", raising=False)
    with pytest.raises(SecretError) as excinfo:
        get_secret("DISCORD_WEBHOOK_URL", required=True)
    message = str(excinfo.value)
    assert "DISCORD_WEBHOOK_URL_FILE" in message
    assert "DISCORD_WEBHOOK_URL_FROM_ENV" in message


class TestRedact:
    def test_never_reveals_full_secret(self):
        secret = "https://discord.com/api/webhooks/123456/abcdefghijklmnop"
        masked = redact(secret)
        assert secret not in masked
        assert "abcdefghijklmnop" not in masked

    def test_handles_short_and_missing_values(self):
        assert redact(None) == "<ikke sat>"
        assert redact("") == "<ikke sat>"
        assert redact("kort") == "****"
