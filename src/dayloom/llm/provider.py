"""OpenAI-compatible vision client.

"Just point it at ``base_url`` + ``api_key`` + ``model``" is true for the happy
path, but every OpenAI-compatible endpoint is a slightly different dialect:
some reject ``response_format``, some choke on ``temperature``, some strip
image parts silently. So the client is written as: one standard request, plus a
small set of *learned* downgrades that are retried immediately instead of
failing the user's whole day of analysis.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ..config import LLMConfig
from .probe import N_CELLS, ProbeResult, make_challenge, parse_colors

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_HTTP_ATTEMPTS = 4

PROBE_QUESTION = (
    "这张图里有 {n} 个色块，从左到右排列。\n"
    '只输出 JSON：{{"colors": [...]}}\n'
    "数组里每个元素是 red / green / blue / yellow 之一，顺序与图中一致。不要解释。"
)


class LLMError(RuntimeError):
    """Base class for anything that goes wrong talking to the model."""


class LLMHTTPError(LLMError):
    def __init__(self, status: int, body: str, url: str = "") -> None:
        self.status = status
        self.body = body
        self.url = url
        hint = http_hint(status)
        prefix = f"HTTP {status}（{hint}）" if hint else f"HTTP {status}"
        super().__init__(f"{prefix} from {url}: {body[:300]}")


#: Status codes the user must fix; retrying only wastes attempts.
FATAL_STATUS = frozenset({401, 402, 403, 404})


def is_fatal_error(exc: BaseException) -> bool:
    """True when retrying cannot help until the user changes something."""
    return isinstance(exc, LLMHTTPError) and exc.status in FATAL_STATUS


def http_hint(status: int) -> str:
    """Turn the status codes people actually hit into a sentence."""
    return {
        401: "API key 无效或没带上",
        402: "账户余额不足",
        403: "无权访问这个模型（可能没开通）",
        404: "端点路径或模型名不对",
        413: "请求体过大（帧数或分辨率太高）",
        429: "被限流或超出配额",
        500: "服务端错误",
        502: "网关错误",
        503: "服务暂不可用",
    }.get(status, "")


class LLMFormatError(LLMError):
    """The endpoint answered, but not with a JSON object we can use."""


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class Part:
    kind: Literal["text", "image"]
    text: str = ""
    data: bytes = b""
    mime: str = "image/jpeg"

    @classmethod
    def of_text(cls, text: str) -> Part:
        return cls(kind="text", text=text)

    @classmethod
    def of_image(cls, data: bytes, mime: str = "image/jpeg") -> Part:
        return cls(kind="image", data=data, mime=mime)

    def to_openai(self) -> dict[str, Any]:
        if self.kind == "text":
            return {"type": "text", "text": self.text}
        b64 = base64.b64encode(self.data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{self.mime};base64,{b64}"}}


@dataclass(slots=True)
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderProfile:
    """What we learned about this endpoint the hard way."""

    supports_json_mode: bool = True
    vision_ok: bool | None = None
    max_frames_hint: int = 12
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "supports_json_mode": self.supports_json_mode,
            "vision_ok": self.vision_ok,
            "max_frames_hint": self.max_frames_hint,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ProviderProfile:
        if not data:
            return cls()
        return cls(
            supports_json_mode=bool(data.get("supports_json_mode", True)),
            vision_ok=data.get("vision_ok"),
            max_frames_hint=int(data.get("max_frames_hint", 12) or 12),
            notes=str(data.get("notes", "")),
        )


class VisionClient:
    def __init__(
        self,
        cfg: LLMConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        profile: ProviderProfile | None = None,
    ) -> None:
        self.cfg = cfg
        self.profile = profile or ProviderProfile()
        self._client = httpx.Client(
            timeout=httpx.Timeout(cfg.timeout, connect=10.0),
            transport=transport,
            follow_redirects=True,
        )
        self._json_mode = self.profile.supports_json_mode

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VisionClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API ------------------------------------------------------
    @property
    def endpoint(self) -> str:
        return f"{self.cfg.base_url}/chat/completions"

    def chat(
        self,
        parts: list[Part],
        *,
        system: str | None = None,
        json_mode: bool | None = None,
        extra_messages: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": self._messages(parts, system, extra_messages),
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        want_json = self._json_mode if json_mode is None else (json_mode and self._json_mode)
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        data = self._post(payload)
        return self._parse_completion(data)

    def complete_json(
        self,
        parts: list[Part],
        *,
        system: str | None = None,
        hint: str = "",
    ) -> tuple[dict[str, Any], Usage]:
        """Ask for JSON; repair once if the model wraps it in prose."""
        first = self.chat(parts, system=system, json_mode=True)
        try:
            return extract_json(first.text), first.usage
        except LLMFormatError:
            log.warning("模型输出不是合法 JSON，追加一次修复指令重试")
            repair = [
                {"role": "assistant", "content": first.text[:4000]},
                {
                    "role": "user",
                    "content": (
                        "上一次的输出不是合法 JSON。" + (hint or "")
                        + "\n请只输出一个 JSON 对象，不要解释，不要 markdown 代码块。"
                    ),
                },
            ]
            second = self.chat(parts, system=system, json_mode=True, extra_messages=repair)
            try:
                return extract_json(second.text), first.usage + second.usage
            except LLMFormatError as exc:
                raise LLMFormatError(
                    f"模型连续两次没有返回合法 JSON（最后一段输出：{second.text[:200]!r}）"
                ) from exc

    # -- probe -----------------------------------------------------------
    def probe(self) -> ProbeResult:
        """Verify the endpoint really reads images. Costs one tiny request."""
        challenge = make_challenge()
        parts = [
            Part.of_text(PROBE_QUESTION.format(n=N_CELLS)),
            Part.of_image(challenge.png, "image/png"),
        ]
        expected = challenge.colors
        try:
            completion = self.chat(parts, json_mode=True, max_tokens=128)
        except LLMHTTPError as exc:
            hint = http_hint(exc.status)
            reason = f"端点返回 HTTP {exc.status}" + (f"（{hint}）" if hint else "") + f"：{exc.body[:180]}"
            return ProbeResult(ok=False, reason=reason, expected=expected)
        except LLMError as exc:
            return ProbeResult(ok=False, reason=f"连不上端点：{exc}", expected=expected)

        detected = parse_colors(completion.text)[:N_CELLS]
        checks: dict[str, bool] = {
            "colors": detected == expected,
            "json": _looks_like_json(completion.text),
        }
        usage = completion.usage
        if usage.prompt_tokens:
            # A 5-cell picture is worth far more than 50 prompt tokens. If the
            # endpoint reports almost nothing, it never looked at the image.
            checks["image_tokens"] = usage.prompt_tokens >= 50
        self.profile.vision_ok = checks["colors"] and checks.get("image_tokens", True)
        if not checks["json"]:
            self._json_mode = False
            self.profile.supports_json_mode = False

        if checks["colors"]:
            reason = "模型能正确读出图片内容（视觉校验通过）"
        else:
            reason = (
                "模型没有读出图片内容：期望 "
                + ",".join(expected)
                + "，实际 "
                + (",".join(detected) if detected else "（无）")
                + "。该模型可能不支持图像输入，或网关把图片丢掉了"
            )
        if not checks.get("image_tokens", True):
            reason += "；用量里几乎没有图片 token，端点很可能忽略了图片"
        if not checks["json"]:
            reason += "；端点忽略了 response_format，已自动降级为提示词约束"
        # JSON mode is a nice-to-have, not a vision requirement: a model that
        # sees the image but ignores response_format is still usable.
        return ProbeResult(
            ok=checks["colors"] and checks.get("image_tokens", True),
            reason=reason,
            expected=expected,
            detected=detected,
            checks=checks,
        )

    # -- internals -------------------------------------------------------
    def _messages(
        self, parts: list[Part], system: str | None, extra: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        content = [p.to_openai() for p in parts]
        if len(content) == 1 and content[0].get("type") == "text":
            messages.append({"role": "user", "content": content[0]["text"]})
        else:
            messages.append({"role": "user", "content": content})
        if extra:
            messages.extend(extra)
        return messages

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(MAX_HTTP_ATTEMPTS):
            try:
                resp = self._client.post(self.endpoint, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == MAX_HTTP_ATTEMPTS - 1:
                    raise LLMError(f"请求失败：{exc}") from exc
                time.sleep(min(8.0, 0.6 * (2**attempt)))
                continue

            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMError(f"端点返回的不是 JSON（{resp.text[:200]!r}）") from exc

            body = resp.text or ""
            if resp.status_code == 400 and self._downgrade(payload, body):
                log.info("端点拒绝了 %s 参数，去掉后重试", self._last_downgrade)
                continue
            if resp.status_code in RETRY_STATUS and attempt < MAX_HTTP_ATTEMPTS - 1:
                time.sleep(min(8.0, 0.6 * (2**attempt)))
                continue
            raise LLMHTTPError(resp.status_code, body, self.endpoint)

        raise LLMError(f"请求失败：{last_error}")  # pragma: no cover

    _last_downgrade = ""

    def _downgrade(self, payload: dict[str, Any], body: str) -> bool:
        """Drop a parameter this endpoint does not understand. Returns True if changed."""
        lowered = body.lower()
        if "response_format" in payload and "response_format" in lowered:
            payload.pop("response_format", None)
            self._json_mode = False
            self.profile.supports_json_mode = False
            self._last_downgrade = "response_format"
            return True
        if "temperature" in payload and "temperature" in lowered:
            payload.pop("temperature", None)
            self._last_downgrade = "temperature"
            return True
        return False

    def _parse_completion(self, data: dict[str, Any]) -> Completion:
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"端点没有返回 choices：{json.dumps(data, ensure_ascii=False)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            ).strip()
        else:
            text = (content or "").strip()
        if not text:
            raise LLMError("端点返回了空内容")
        return Completion(text=text, usage=usage, raw=data)


def _looks_like_json(text: str) -> bool:
    try:
        extract_json(text)
        return True
    except LLMFormatError:
        return False


def extract_json(text: str) -> dict[str, Any]:
    """Find the first JSON object in a model reply."""
    raw = (text or "").strip()
    if not raw:
        raise LLMFormatError("模型返回空内容")
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    candidates = [cleaned.strip()]
    obj = _balanced_object(cleaned)
    if obj:
        candidates.append(obj)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LLMFormatError(f"没有在回复里找到 JSON 对象：{raw[:200]!r}")


def _balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
