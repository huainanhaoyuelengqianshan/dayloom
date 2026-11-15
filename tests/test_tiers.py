"""Model tiers, the quality gate and escalation."""

from __future__ import annotations

import pytest
from conftest import add_frame, at

from dayloom.analyzer.pipeline import analyze_with_escalation
from dayloom.analyzer.quality import QualityReport, assess
from dayloom.config import LLMConfig, LLMTier
from dayloom.llm.mockvlm import MockVLM
from dayloom.llm.provider import VisionClient
from dayloom.store.repo import NewActivity

DAY = "2026-05-10"


def activity(start: float, end: float, *, confidence=0.8, category="coding", summary="在改代码",
             app="code", title="main.py") -> NewActivity:
    return NewActivity(
        start_ts=start,
        end_ts=end,
        app=app,
        category=category,
        title=title,
        summary=summary,
        confidence=confidence,
    )


# -- quality -----------------------------------------------------------
def test_quality_rewards_real_descriptions():
    good = assess([activity(0, 600, confidence=0.9, summary="在改截图去重阈值")])
    assert good.mean_confidence == 0.9
    assert good.unknown_ratio == 0.0
    assert good.shallow_ratio == 0.0
    assert good.score > 0.8


def test_quality_penalises_empty_summaries():
    shallow = assess([activity(0, 600, confidence=0.9, summary="code", app="code")])
    assert shallow.shallow_ratio == 1.0
    assert shallow.score < 0.5


def test_quality_counts_unknown_time():
    report = assess(
        [
            activity(0, 300, confidence=0.8),
            activity(300, 900, confidence=0.8, category="unknown", summary="画面无法辨认"),
        ]
    )
    assert abs(report.unknown_ratio - 600 / 900) < 0.01


def test_gate_reports_the_reason():
    report = QualityReport(segments=2, mean_confidence=0.2, unknown_ratio=0.8, shallow_ratio=0.9)

    class Gate:
        min_confidence = 0.45
        max_unknown_ratio = 0.35
        max_shallow_ratio = 0.6

    reason = report.failure(Gate)  # type: ignore[arg-type]
    assert reason and "置信度" in reason and "unknown" in reason and "空泛" in reason


def test_gate_passes_a_good_result():
    report = QualityReport(segments=2, mean_confidence=0.9, unknown_ratio=0.0, shallow_ratio=0.1)

    class Gate:
        min_confidence = 0.45
        max_unknown_ratio = 0.35
        max_shallow_ratio = 0.6

    assert report.failure(Gate) is None  # type: ignore[arg-type]


# -- config ------------------------------------------------------------
def test_tier_chain_starts_with_primary(cfg):
    cfg.llm.base_url = "http://local/v1"
    cfg.llm.model = "small"
    chain = cfg.llm.tier_chain()
    assert len(chain) == 1
    assert chain[0][0] == "primary"
    assert chain[0][1].model == "small"


def test_tier_chain_appends_escalation_targets(cfg):
    cfg.llm.tiers = [
        LLMTier(name="cloud", base_url="https://cloud/v1", model="big", max_frames=10),
        LLMTier(base_url="https://other/v1", model="bigger"),
    ]
    chain = cfg.llm.tier_chain()
    assert [name for name, _ in chain] == ["primary", "cloud", "tier2"]
    assert chain[1][1].max_frames == 10
    assert chain[1][1].base_url == "https://cloud/v1"


# -- escalation --------------------------------------------------------
def _batch_with_frames(cfg, repo, count=6):
    for i in range(count):
        add_frame(
            repo, cfg.home, at(10, 0) + i * 60, app="code", title=f"file{i}.py", dhash_value=i
        )
    repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert repo.claim_batch(batch.id)
    return batch


def test_escalates_when_the_cheap_tier_is_sloppy(cfg, repo):
    cfg.llm.quality.min_confidence = 0.5
    with MockVLM(confidence=0.2, summary_style="shallow") as weak, MockVLM() as strong:
        cfg.llm.base_url = weak.base_url
        cfg.llm.model = "weak"
        cfg.llm.tiers = [
            LLMTier(name="strong", base_url=strong.base_url, model="strong", max_frames=8)
        ]
        batch = _batch_with_frames(cfg, repo)
        clients = {
            "primary": VisionClient(cfg.llm),
            "strong": VisionClient(cfg.llm.tiers[0].to_llm()),
        }
        try:
            result = analyze_with_escalation(
                cfg=cfg, repo=repo, batch=batch, clients=clients
            )
        finally:
            for client in clients.values():
                client.close()

    assert result.escalated is True
    assert result.tier == "strong"
    assert [a.tier for a in result.attempts] == ["primary", "strong"]
    assert result.attempts[0].ok is False

    stored = repo.activities_between(at(10, 0), at(10, 30))
    assert stored
    assert all("mock 分析" in a.summary for a in stored)
    model = repo.db.scalar("SELECT model FROM batches WHERE id = ?", (batch.id,))
    assert model == "strong"


def test_no_escalation_when_the_cheap_tier_is_fine(cfg, repo):
    with MockVLM() as only:
        cfg.llm.base_url = only.base_url
        cfg.llm.model = "cheap"
        batch = _batch_with_frames(cfg, repo)
        clients = {"primary": VisionClient(cfg.llm)}
        try:
            result = analyze_with_escalation(
                cfg=cfg, repo=repo, batch=batch, clients=clients
            )
        finally:
            for client in clients.values():
                client.close()

    assert result.escalated is False
    assert result.tier == "primary"
    assert result.quality_ok is True
    model = repo.db.scalar("SELECT model FROM batches WHERE id = ?", (batch.id,))
    assert model == "cheap"


def test_a_failing_tier_falls_through_to_the_next(cfg, repo):
    cfg.llm.base_url = "http://127.0.0.1:1/v1"  # nothing listens here
    cfg.llm.model = "dead"
    with MockVLM() as alive:
        cfg.llm.tiers = [LLMTier(name="alive", base_url=alive.base_url, model="alive")]
        batch = _batch_with_frames(cfg, repo)
        clients = {
            "primary": VisionClient(cfg.llm),
            "alive": VisionClient(cfg.llm.tiers[0].to_llm()),
        }
        try:
            result = analyze_with_escalation(
                cfg=cfg, repo=repo, batch=batch, clients=clients
            )
        finally:
            for client in clients.values():
                client.close()

    assert result.tier == "alive"
    assert result.attempts[0].ok is False


def test_all_tiers_failing_raises(cfg, repo):
    from dayloom.llm.provider import LLMError

    cfg.llm.base_url = "http://127.0.0.1:1/v1"
    cfg.llm.model = "dead"
    batch = _batch_with_frames(cfg, repo)
    clients = {"primary": VisionClient(cfg.llm)}
    try:
        try:
            analyze_with_escalation(cfg=cfg, repo=repo, batch=batch, clients=clients)
        except LLMError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected an LLMError")
    finally:
        for client in clients.values():
            client.close()


def test_llm_config_defaults_have_no_tiers():
    cfg = LLMConfig()
    assert cfg.tiers == []
    assert cfg.quality.min_confidence == 0.45


# -- switching to a local model ---------------------------------------
def test_a_broken_escalation_tier_does_not_block_the_primary(cfg, repo):
    """Regression: an unfunded cloud tier aborted analysis of a working local model."""
    from dayloom.analyzer.pipeline import build_clients

    with MockVLM() as alive:
        cfg.llm.base_url = alive.base_url
        cfg.llm.model = "local"
        cfg.llm.tiers = [LLMTier(name="dead", base_url="http://127.0.0.1:1/v1", model="dead")]
        clients = build_clients(cfg, repo)
        try:
            assert "primary" in clients
            assert "dead" not in clients
        finally:
            for client in clients.values():
                client.close()


def test_all_tiers_broken_raises(cfg, repo):
    from dayloom.analyzer.pipeline import build_clients
    from dayloom.llm.session import VisionUnavailable

    cfg.llm.base_url = "http://127.0.0.1:1/v1"
    cfg.llm.model = "dead"
    cfg.llm.tiers = []
    with pytest.raises(VisionUnavailable):
        build_clients(cfg, repo)


def test_with_local_model_keeps_the_cloud_as_a_tier(cfg):
    from dayloom import config as config_mod

    cfg.llm.base_url = "https://api.example.com/v1"
    cfg.llm.model = "cloud-vision"
    cfg.llm.api_key = "sk-test"

    updated = config_mod.with_local_model(cfg, "qwen2.5vl:3b", "http://127.0.0.1:11434/v1")
    assert updated.llm.base_url == "http://127.0.0.1:11434/v1"
    assert updated.llm.model == "qwen2.5vl:3b"
    assert updated.llm.api_key == ""
    assert updated.llm.max_frames <= 4
    # CPU inference is slow; the 120s default cancelled every local request.
    assert updated.llm.timeout >= 600
    assert [t.model for t in updated.llm.tiers] == ["cloud-vision"]
    assert updated.llm.tiers[0].api_key == "sk-test"


def test_with_local_model_does_not_duplicate_tiers(cfg):
    from dayloom import config as config_mod

    cfg.llm.base_url = "http://127.0.0.1:8080/v1"
    cfg.llm.model = "qwen2.5vl-3b"
    once = config_mod.with_local_model(cfg, "qwen2.5vl-7b", "http://127.0.0.1:8080/v1")
    twice = config_mod.with_local_model(once, "qwen2.5vl-7b", "http://127.0.0.1:8080/v1")
    assert twice.llm.tiers == []
    assert twice.llm.model == "qwen2.5vl-7b"
