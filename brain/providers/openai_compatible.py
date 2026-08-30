from __future__ import annotations

from collections.abc import Callable, Sequence
import json
from urllib import request

from brain.model_reasoner import ChatMessage


Transport = Callable[[request.Request, float], bytes]


def _default_transport(req: request.Request, timeout: float) -> bytes:
    with request.urlopen(req, timeout=timeout) as response:
        return response.read()


class OpenAICompatibleProvider:
    """Small OpenAI-compatible chat-completions adapter.

    It works with providers exposing the conventional /v1/chat/completions
    shape, including local vLLM deployments. Credentials remain runtime data
    and are never stored by this adapter.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        temperature: float = 0.3,
        timeout: float = 30.0,
        transport: Transport = _default_transport,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = float(temperature)
        self.timeout = float(timeout)
        self.transport = transport

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def complete(self, messages: Sequence[ChatMessage]) -> str:
        return self._complete(messages)

    def complete_json(self, messages: Sequence[ChatMessage]) -> str:
        """Request one strict JSON object without changing normal chat behavior."""

        return self._complete(
            messages,
            response_format={"type": "json_object"},
            temperature=0.0,
        )

    def _complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        response_format: dict[str, str] | None = None,
        temperature: float | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "temperature": self.temperature if temperature is None else temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        raw = self.transport(req, self.timeout)

        try:
            response = json.loads(raw.decode("utf-8"))
            content = response["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("invalid OpenAI-compatible chat completion response") from exc

        if not isinstance(content, str):
            raise RuntimeError("chat completion content must be a string")
        return content
