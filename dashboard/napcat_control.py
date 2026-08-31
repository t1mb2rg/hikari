from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from resident.napcat_login_guard import NapCatLoginError, NapCatWebUIConfig


class NapCatDashboardControl:
    """Tiny authenticated NapCat WebUI control surface used by the local dashboard."""

    def __init__(self, root: str | Path, *, timeout_seconds: float = 3.0) -> None:
        self.root = Path(root).expanduser().resolve()
        self.timeout_seconds = float(timeout_seconds)
        self._config: NapCatWebUIConfig | None = None
        self._credential: str | None = None
        self._credential_expires_at = 0.0

    def _current_config(self) -> NapCatWebUIConfig:
        config = NapCatWebUIConfig.from_root(self.root)
        if self._config != config:
            self._config = config
            self._credential = None
            self._credential_expires_at = 0.0
        return config

    def _post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        credential: str | None = None,
    ) -> object:
        config = self._current_config()
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        request = Request(
            f"{config.base_url}{path}",
            data=json.dumps(dict(body), separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            response = urlopen(request, timeout=self.timeout_seconds)
            with response:
                raw = response.read(64 * 1024 + 1)
        except (HTTPError, URLError, OSError, TimeoutError):
            raise NapCatLoginError("NapCat WebUI request failed") from None
        if len(raw) > 64 * 1024:
            raise NapCatLoginError("NapCat WebUI response exceeded size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NapCatLoginError("NapCat WebUI returned malformed JSON") from None
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise NapCatLoginError("NapCat WebUI API rejected the request")
        return payload.get("data")

    def _authenticate(self) -> str:
        config = self._current_config()
        token_hash = hashlib.sha256(
            f"{config.token}.napcat".encode("utf-8")
        ).hexdigest()
        data = self._post("/api/auth/login", {"hash": token_hash})
        if not isinstance(data, Mapping):
            raise NapCatLoginError("NapCat WebUI credential was missing")
        credential = data.get("Credential")
        if not isinstance(credential, str) or not credential:
            raise NapCatLoginError("NapCat WebUI credential was missing")
        self._credential = credential
        self._credential_expires_at = time.monotonic() + 50 * 60
        return credential

    def _active_credential(self) -> str:
        if self._credential and time.monotonic() < self._credential_expires_at:
            return self._credential
        return self._authenticate()

    def refresh_qrcode(self) -> None:
        self._post(
            "/api/QQLogin/RefreshQRcode",
            {},
            credential=self._active_credential(),
        )
