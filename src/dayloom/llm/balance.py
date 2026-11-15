"""Account balance, where the provider exposes it.

DeepSeek publishes ``GET /user/balance``; most OpenAI-compatible endpoints do
not, so this is a "best effort, say so plainly" helper rather than a promise.
"""

from __future__ import annotations

import time

import httpx

from ..config import LLMConfig


def supports_balance(base_url: str) -> bool:
    return "deepseek.com" in base_url.lower()


def fetch_balance(llm: LLMConfig, *, timeout: float = 15.0) -> tuple[bool, str]:
    """``(is_available, readable_text)``; never raises."""
    if not llm.configured:
        return False, "还没配置模型"
    if not supports_balance(llm.base_url):
        return False, f"这个端点不提供余额查询：{llm.base_url}"

    root = llm.base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    url = f"{root}/user/balance"
    headers = {"Authorization": f"Bearer {llm.api_key}"}
    # The link to an overseas endpoint drops connections often enough that a
    # single failed GET would make this command look broken.
    last_error = ""
    for attempt in range(3):
        try:
            with httpx.Client(timeout=min(timeout, 20.0)) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(1.0 + attempt)
                continue
            return False, f"查不到余额：{last_error}"
        break

    if response.status_code == 401:
        return False, "API key 无效（401）"
    if response.status_code >= 400:
        return False, f"查询失败：HTTP {response.status_code} {response.text[:120]}"

    try:
        data = response.json()
    except ValueError:
        return False, "余额接口返回的不是 JSON"

    available = bool(data.get("is_available", True))
    parts = []
    for info in data.get("balance_infos") or []:
        currency = info.get("currency") or "?"
        total = info.get("total_balance")
        granted = info.get("granted_balance")
        piece = f"{currency} {total}"
        if granted not in (None, "0", 0, "0.00"):
            piece += f"（赠金 {granted}）"
        parts.append(piece)
    text = "；".join(parts) if parts else "余额未知"
    return available, text
