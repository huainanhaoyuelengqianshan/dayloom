"""Tiered analysis: cheap model first, escalate when the result is poor.

Order comes from ``llm.tier_chain()``: the ``[llm]`` block is the primary tier
(usually a local model) and ``[[llm.tiers]]`` are escalation targets in order.

Rules, in one place so the daemon and the CLI cannot drift apart:

* a tier that raises is skipped, the next one runs;
* a tier that answers but scores below the quality gate is recorded and the
  next one runs;
* the last good-enough answer wins (it has already replaced the earlier one in
  the database — ``activities`` are keyed by ``(batch_id, seq)``);
* if every tier failed with an error, the last error is raised so the batch is
  marked failed and retried later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Config, LLMConfig
from ..llm.provider import LLMError, VisionClient
from ..store.repo import BatchRow, Repo
from .segment import SegmentOutcome, analyze_batch

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TierAttempt:
    tier: str
    model: str
    ok: bool
    reason: str = ""


@dataclass(slots=True)
class EscalationResult:
    outcome: SegmentOutcome
    tier: str
    attempts: list[TierAttempt] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        return len(self.attempts) > 1

    @property
    def quality_ok(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].ok


def analyze_with_escalation(
    *,
    cfg: Config,
    repo: Repo,
    batch: BatchRow,
    clients: dict[str, VisionClient],
    analyzer=analyze_batch,
) -> EscalationResult:
    attempts: list[TierAttempt] = []
    last_error: LLMError | None = None
    last_outcome: tuple[SegmentOutcome, str] | None = None
    gate = cfg.llm.quality

    for name, llm in cfg.llm.tier_chain():
        client = clients.get(name)
        if client is None:
            continue
        try:
            outcome = analyzer(cfg=cfg, repo=repo, client=client, batch=batch, llm=llm)
        except LLMError as exc:
            last_error = exc
            attempts.append(TierAttempt(name, llm.model, False, str(exc)[:200]))
            log.warning("批次 %s 在「%s」失败，尝试下一档：%s", batch.id, name, exc)
            continue

        last_error = None
        last_outcome = (outcome, name)
        failure = outcome.quality.failure(gate) if outcome.quality else None
        attempts.append(TierAttempt(name, llm.model, failure is None, failure or ""))
        if failure is None:
            return EscalationResult(outcome=outcome, tier=name, attempts=attempts)
        log.info("批次 %s 在「%s」质量不足（%s），升级", batch.id, name, failure)

    if last_outcome is not None:
        outcome, name = last_outcome
        return EscalationResult(outcome=outcome, tier=name, attempts=attempts)
    if last_error is not None:
        raise last_error
    raise LLMError("没有任何可用的模型档位")


def _short(text: str, limit: int = 100) -> str:
    """One line, no JSON dumps, no newlines — for user-facing notes."""
    return " ".join(str(text).split())[:limit]


def build_clients(
    cfg: Config,
    repo: Repo,
    *,
    verify: bool = True,
    skipped: list[str] | None = None,
) -> dict[str, VisionClient]:
    """One client per tier, skipping tiers that fail verification.

    A broken *escalation* tier must not block the working primary: an unfunded
    cloud key used to abort every analysis even though the local model was fine.
    Only when nothing is usable does this raise.
    """
    from ..llm.session import VisionUnavailable, open_client

    clients: dict[str, VisionClient] = {}
    problems: list[str] = []
    skipped = skipped if skipped is not None else []
    for name, llm in cfg.llm.tier_chain():
        try:
            clients[name] = open_client(llm, repo, name=name, verify=verify)
        except VisionUnavailable as exc:
            problems.append(f"{name}: {_short(exc.reason)}")
            # INFO, not WARNING: a broken *backup* tier is not an error and must
            # not make `dayloom analyze` look like it failed.
            log.info("档位「%s」不可用，跳过：%s", name, exc.reason)
    if not clients:
        raise VisionUnavailable("所有模型档位都不可用：" + "；".join(problems))
    if problems:
        log.debug("跳过的档位：%s", "；".join(problems))
    skipped.extend(problems)
    return clients


def describe_chain(cfg: Config) -> list[tuple[str, LLMConfig]]:
    return cfg.llm.tier_chain()
