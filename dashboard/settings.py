from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
from threading import RLock
from urllib.parse import urlparse
from uuid import uuid4

from dotenv import dotenv_values
from resident.file_locks import serialized_file_update


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: str = "text"
    default: str = ""
    choices: tuple[str, ...] = ()


SETTINGS = (
    Setting("HIKARI_MODEL_BASE_URL", "模型 API 地址", "conversation", "url"),
    Setting("HIKARI_MODEL_NAME", "对话模型", "conversation"),
    Setting("HIKARI_MODEL_API_KEY", "模型 API Key", "conversation", "secret"),
    Setting("HIKARI_ENGINEERING_ENABLED", "启用工程能力", "engineering", "bool", "false"),
    Setting("HIKARI_ENGINEERING_BACKEND", "工程后端", "engineering", "choice", "claude", ("claude", "codex")),
    Setting("HIKARI_ENGINEERING_MODEL", "Claude 工程模型", "engineering", default="sonnet"),
    Setting("HIKARI_ENGINEERING_CODEX_MODEL", "Codex 模型（留空使用本机配置）", "engineering"),
    Setting("HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS", "单次工程超时（秒）", "engineering", "positive", "300"),
    Setting("HIKARI_ENGINEERING_MAX_TURNS", "Claude 最大轮数", "engineering", "integer", "30"),
    Setting("HIKARI_QQ_ENABLED", "启用 QQ", "qq", "bool", "false"),
    Setting("HIKARI_ONEBOT_ALLOWED_USER_IDS", "私人用户白名单", "qq", "ids"),
    Setting("HIKARI_ONEBOT_ALLOWED_GROUP_IDS", "群白名单", "qq", "ids"),
    Setting("HIKARI_ONEBOT_ALLOWED_GROUP_USER_IDS", "仅群聊成员白名单", "qq", "ids"),
    Setting("HIKARI_ONEBOT_ACCESS_TOKEN", "OneBot Token", "qq", "secret"),
    Setting("HIKARI_CONVERSATION_SHARED_SECRET", "Conversation 通道密钥", "qq", "secret"),
    Setting("HIKARI_PRESENCE_CHANNEL", "主动提醒通道", "presence", "choice", "windows", ("windows", "qq")),
    Setting("HIKARI_GITHUB_REPOSITORY", "GitHub 仓库（留空使用 origin）", "github", "repository"),
    Setting("HIKARI_GITHUB_ALLOWED_REPOSITORIES", "允许访问的远端仓库（逗号分隔）", "github", "repositories"),
)
_SCHEMA = {setting.key: setting for setting in SETTINGS}
_WRITE_LOCK = RLock()


class SettingsConflict(ValueError):
    pass


class DashboardSettings:
    """Operator-owned dotenv editing with revision checks and write-only secrets.

    This is configuration, not live runtime truth. Saving never changes os.environ
    or restarts a process. Existing comments and unknown settings are preserved.
    """

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    def _read(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""

    @staticmethod
    def _revision(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def values(self) -> dict[str, str]:
        parsed = dotenv_values(stream=io.StringIO(self._read().decode("utf-8-sig")), interpolate=False)
        return {key: str(value) for key, value in parsed.items() if value is not None}

    def snapshot(self) -> dict:
        raw = self._read()
        values = dotenv_values(stream=io.StringIO(raw.decode("utf-8-sig")), interpolate=False)
        return {
            "revision": self._revision(raw), "path": str(self.path),
            "apply_mode": "restart_required", "runtime_applied": False,
            "fields": [
                {"key": item.key, "label": item.label, "group": item.group,
                 "kind": item.kind, "choices": list(item.choices),
                 "value": "" if item.kind == "secret" else values.get(item.key, item.default),
                 "configured": bool(values.get(item.key)),
                 "process_override": item.key in os.environ}
                for item in SETTINGS
            ],
        }

    def save(self, changes: dict, revision: str) -> dict:
        if not isinstance(changes, dict) or not changes:
            raise ValueError("请提供需要修改的配置")
        normalized: dict[str, str] = {}
        for key, value in changes.items():
            item = _SCHEMA.get(key)
            if item is None:
                raise ValueError("包含不允许通过面板修改的配置项")
            if not isinstance(value, str) or any(ch in value for ch in "\r\n\x00") or len(value) > 4096:
                raise ValueError(f"{item.label} 格式不正确")
            value = value.strip()
            if "${" in value:
                raise ValueError(f"{item.label} 不支持变量插值，请填写最终配置值")
            if item.kind == "secret" and not value:
                continue  # Empty secret fields preserve the existing credential.
            if item.kind == "bool" and value not in {"true", "false"}:
                raise ValueError(f"{item.label} 必须为 true 或 false")
            if item.kind == "ids" and value and not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", value):
                raise ValueError(f"{item.label} 必须为逗号分隔的数字")
            if item.kind == "choice" and value not in item.choices:
                raise ValueError(f"{item.label} 选项不正确")
            if item.kind in {"positive", "integer"}:
                try:
                    number = float(value)
                    if not 0 < number < 1_000_000 or (item.kind == "integer" and not number.is_integer()):
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{item.label} 必须为有效正数") from None
                if item.kind == "integer":
                    value = str(int(number))
            if item.kind == "url" and value:
                parsed = urlparse(value)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("API 地址必须是无内嵌凭据的 HTTP(S) URL")
            if item.kind == "repository" and value:
                from integrations.github.client import validate_repository
                validate_repository(value)
            if item.kind == "repositories" and value:
                from integrations.github.client import validate_repository
                value = ",".join(dict.fromkeys(validate_repository(part.strip()) for part in value.split(",")))
            normalized[key] = value

        with _WRITE_LOCK, serialized_file_update(self.path):
            raw = self._read()
            if revision != self._revision(raw):
                raise SettingsConflict("配置已被其他操作更新，请重新加载后再保存")
            text = raw.decode("utf-8-sig")
            pending = dict(normalized)
            lines = []
            for line in text.splitlines():
                match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
                key = match.group(1) if match else None
                if key in normalized:
                    # Replace duplicate definitions too: dotenv uses the last one.
                    lines.append(f"{key}={json.dumps(normalized[key], ensure_ascii=False)}")
                    pending.pop(key, None)
                else:
                    lines.append(line)
            lines.extend(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in pending.items())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return {"ok": True, "changed_keys": list(normalized), "restart_required": True,
                "message": "配置已保存；运行中的进程尚未应用，需要受控重启。", **self.snapshot()}
