import sqlite3
from pathlib import Path

import pytest

from invest_vault.ai_providers import DirectAPIClient, EncryptedCredentialStore
from invest_vault.ledger import Vault


def test_api_keys_are_encrypted_in_sqlite_and_masked_at_the_api_boundary(tmp_path: Path) -> None:
    database = tmp_path / "vault.sqlite3"
    with Vault(database) as vault:
        store = EncryptedCredentialStore(vault, tmp_path / "ai-master.key")
        saved = store.set_api_key("openai", "sk-test-secret-9876")

        assert saved == {"provider_id": "openai", "configured": True, "masked": "••••9876"}
        assert store.get_api_key("openai") == "sk-test-secret-9876"
        assert store.list_configured()["openai"]["masked"] == "••••9876"

    raw_database = database.read_bytes()
    assert b"sk-test-secret-9876" not in raw_database
    with sqlite3.connect(database) as connection:
        envelope = connection.execute(
            "SELECT encrypted_secret FROM ai_provider_credentials WHERE provider_id = 'openai'"
        ).fetchone()[0]
    assert envelope.startswith("v1:")
    assert (tmp_path / "ai-master.key").stat().st_mode & 0o777 == 0o600


def test_api_key_encryption_binds_ciphertext_to_provider(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        store = EncryptedCredentialStore(vault, tmp_path / "ai-master.key")
        store.set_api_key("openai", "sk-openai-secret")
        envelope = vault.connection.execute(
            "SELECT encrypted_secret FROM ai_provider_credentials WHERE provider_id = 'openai'"
        ).fetchone()[0]
        vault.connection.execute(
            "INSERT INTO ai_provider_credentials VALUES ('anthropic', ?, 'cret', CURRENT_TIMESTAMP)",
            (envelope,),
        )
        vault.connection.commit()

        with pytest.raises(ValueError, match="无法解密"):
            store.get_api_key("anthropic")


def test_direct_openai_client_requests_json_and_parses_the_structured_reply() -> None:
    captured = {}

    def send(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return {"choices": [{"message": {"content": '{"content":"ok","unknowns":[]}'}}]}

    client = DirectAPIClient("openai", "sk-local", send=send)
    result = client.generate_json(
        model="gpt-5.2",
        system="只使用提供的证据",
        prompt="分析腾讯控股",
        schema={"type": "object"},
    )

    assert result == {"content": "ok", "unknowns": []}
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-local"
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_direct_provider_errors_never_echo_the_api_key() -> None:
    def send(*_args, **_kwargs):
        raise RuntimeError("authorization failed for sk-super-secret")

    client = DirectAPIClient("deepseek", "sk-super-secret", send=send)
    with pytest.raises(RuntimeError) as error:
        client.generate_json(model="deepseek-v4-flash", system="system", prompt="prompt", schema={})

    assert "sk-super-secret" not in str(error.value)
