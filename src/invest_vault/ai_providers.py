"""Encrypted local API credentials and small direct model-provider clients."""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .ledger import Vault

PROVIDER_CATALOG: dict[str, dict[str, object]] = {
    "codex": {
        "name": "Codex 登录态",
        "auth_kind": "codex_login",
        "models": [],
    },
    "openai": {
        "name": "OpenAI API",
        "auth_kind": "api_key",
        "models": ["gpt-5.2"],
    },
    "anthropic": {
        "name": "Anthropic API",
        "auth_kind": "api_key",
        "models": ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5"],
    },
    "google": {
        "name": "Google Gemini API",
        "auth_kind": "api_key",
        "models": ["gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro"],
    },
    "deepseek": {
        "name": "DeepSeek API",
        "auth_kind": "api_key",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
    },
}


class EncryptedCredentialStore:
    """Keep only AES-256-GCM envelopes in SQLite; the 0600 master key stays outside it."""

    def __init__(self, vault: Vault, key_path: Path) -> None:
        self.vault = vault
        self.key_path = Path(key_path)

    def _master_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError:
            key = os.urandom(32)
            try:
                descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = self.key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key)
        if len(key) != 32:
            raise ValueError("AI 密钥主密钥文件无效")
        os.chmod(self.key_path, 0o600)
        return key

    @staticmethod
    def _aad(provider_id: str) -> bytes:
        return f"invest-vault:v1:{provider_id}".encode()

    def _encrypt(self, provider_id: str, plaintext: str) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._master_key()).encrypt(nonce, plaintext.encode(), self._aad(provider_id))
        return "v1:" + ":".join(base64.b64encode(value).decode() for value in (nonce, ciphertext))

    def _decrypt(self, provider_id: str, envelope: str) -> str:
        try:
            version, nonce, ciphertext = envelope.split(":", 2)
            if version != "v1":
                raise ValueError
            plaintext = AESGCM(self._master_key()).decrypt(
                base64.b64decode(nonce),
                base64.b64decode(ciphertext),
                self._aad(provider_id),
            )
            return plaintext.decode()
        except Exception as error:
            raise ValueError(f"{provider_id} API key 无法解密，请删除后重新填写") from error

    def set_api_key(self, provider_id: str, key: str) -> dict[str, object]:
        if provider_id not in PROVIDER_CATALOG or provider_id == "codex":
            raise ValueError("不支持为该 Provider 保存 API key")
        cleaned = key.strip()
        if not cleaned or len(cleaned) > 1_000:
            raise ValueError("API key 不能为空且不得超过1000字符")
        envelope = self._encrypt(provider_id, cleaned)
        suffix = cleaned[-4:]
        now = datetime.now(timezone.utc).isoformat()
        with self.vault.lock:
            self.vault.connection.execute(
                "INSERT INTO ai_provider_credentials VALUES (?, ?, ?, ?) "
                "ON CONFLICT(provider_id) DO UPDATE SET encrypted_secret = excluded.encrypted_secret, "
                "masked_suffix = excluded.masked_suffix, updated_at = excluded.updated_at",
                (provider_id, envelope, suffix, now),
            )
            self.vault.connection.commit()
        return {"provider_id": provider_id, "configured": True, "masked": f"••••{suffix}"}

    def get_api_key(self, provider_id: str) -> str | None:
        with self.vault.lock:
            row = self.vault.connection.execute(
                "SELECT encrypted_secret FROM ai_provider_credentials WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        return self._decrypt(provider_id, str(row["encrypted_secret"])) if row else None

    def delete_api_key(self, provider_id: str) -> None:
        if provider_id == "codex":
            raise ValueError("Codex 登录态由 Codex 管理，不能在此删除")
        with self.vault.lock:
            self.vault.connection.execute(
                "DELETE FROM ai_provider_credentials WHERE provider_id = ?", (provider_id,)
            )
            self.vault.connection.commit()

    def list_configured(self) -> dict[str, dict[str, object]]:
        with self.vault.lock:
            rows = self.vault.connection.execute(
                "SELECT provider_id, masked_suffix, updated_at FROM ai_provider_credentials"
            ).fetchall()
        return {
            str(row["provider_id"]): {
                "configured": True,
                "masked": f"••••{row['masked_suffix']}",
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        }


Send = Callable[..., dict[str, Any]]


class DirectAPIClient:
    """Minimal JSON-only clients for the four supported bring-your-own-key APIs."""

    def __init__(
        self,
        provider_id: str,
        api_key: str,
        *,
        send: Send | None = None,
        timeout: float = 300.0,
    ) -> None:
        if provider_id not in PROVIDER_CATALOG or provider_id == "codex":
            raise ValueError("不支持的直接 API Provider")
        self.provider_id = provider_id
        self.api_key = api_key
        self.send = send or self._send
        self.timeout = timeout

    @staticmethod
    def _send(url: str, **kwargs: object) -> dict[str, Any]:
        response = requests.post(url, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Provider 返回格式无效")
        return payload

    @staticmethod
    def _json_text(text: str) -> dict[str, object]:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
        value = json.loads(cleaned)
        if not isinstance(value, dict):
            raise ValueError("结构化输出必须是 JSON object")
        return value

    def generate_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        schema_instruction = (
            "\n只返回符合以下 JSON Schema 的 JSON object，不要使用 Markdown 代码块：\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            if self.provider_id in {"openai", "deepseek"}:
                base = (
                    "https://api.openai.com/v1"
                    if self.provider_id == "openai"
                    else "https://api.deepseek.com"
                )
                payload = self.send(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system + schema_instruction},
                            {"role": "user", "content": prompt},
                        ],
                        "response_format": {"type": "json_object"},
                    },
                    timeout=self.timeout,
                )
                text = str(payload["choices"][0]["message"]["content"])
            elif self.provider_id == "anthropic":
                payload = self.send(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 16_384,
                        "system": system + schema_instruction,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=self.timeout,
                )
                text = "".join(
                    str(item.get("text") or "")
                    for item in payload.get("content") or []
                    if item.get("type") == "text"
                )
            else:
                payload = self.send(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "responseJsonSchema": schema,
                        },
                    },
                    timeout=self.timeout,
                )
                text = "".join(
                    str(item.get("text") or "") for item in payload["candidates"][0]["content"]["parts"]
                )
            return self._json_text(text)
        except Exception as error:
            detail = str(error).replace(self.api_key, "[hidden]")
            raise RuntimeError(
                f"{PROVIDER_CATALOG[self.provider_id]['name']} 请求失败：{detail[:240]}"
            ) from error
