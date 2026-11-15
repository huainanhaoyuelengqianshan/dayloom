"""Client lifecycle + the "verify the endpoint can see" gate.

Every endpoint is verified once and the result (including the quirks we learned,
like "this one ignores response_format") is cached in the database, keyed by
``base_url|model``. That makes tiered setups cheap: the local model and the
cloud escalation target are each probed exactly once.
"""

from __future__ import annotations

import logging

from .. import clock
from ..config import LLMConfig
from ..store.repo import Repo
from .provider import ProviderProfile, VisionClient

log = logging.getLogger(__name__)

NEGATIVE_CACHE_SECONDS = 600.0
PROBES_KEY = "llm_probes"


class VisionUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _key(llm: LLMConfig) -> str:
    return f"{llm.base_url}|{llm.model}"


def _probes(repo: Repo) -> dict:
    data = repo.get_setting(PROBES_KEY)
    return data if isinstance(data, dict) else {}


def probe_entry(repo: Repo, llm: LLMConfig) -> dict | None:
    return _probes(repo).get(_key(llm))


def probe_summary(repo: Repo, cfg) -> dict | None:
    """Primary tier's cached probe, for ``status`` and the GUI."""
    return probe_entry(repo, cfg.llm)


def probe_and_cache(
    llm: LLMConfig, repo: Repo, *, name: str = "primary"
) -> tuple[VisionClient, object]:
    """Always run a fresh probe and remember the outcome. Returns (client, result)."""
    probes = _probes(repo)
    key = _key(llm)
    entry = probes.get(key) or {}
    client = VisionClient(llm, profile=ProviderProfile.from_dict(entry.get("profile")))
    result = client.probe()
    probes[key] = {
        "ok": result.ok,
        "reason": result.reason,
        "at": clock.now_ts(),
        "name": name,
        "model": llm.model,
        "profile": client.profile.as_dict(),
    }
    repo.set_setting(PROBES_KEY, probes)
    return client, result


def open_client(
    llm: LLMConfig,
    repo: Repo,
    *,
    name: str = "primary",
    verify: bool = True,
) -> VisionClient:
    """Build a client, verifying vision support once per (base_url, model)."""
    probes = _probes(repo)
    key = _key(llm)
    entry = probes.get(key) or {}
    if not verify or entry.get("ok"):
        profile = ProviderProfile.from_dict(entry.get("profile"))
        return VisionClient(llm, profile=profile)
    if entry and (clock.now_ts() - float(entry.get("at") or 0)) < NEGATIVE_CACHE_SECONDS:
        raise VisionUnavailable(str(entry.get("reason") or "该模型不支持图像输入"))

    client, result = probe_and_cache(llm, repo, name=name)
    if not result.ok:
        raise VisionUnavailable(result.reason)
    log.info("视觉校验通过（%s）：%s", name, llm.model)
    return client
