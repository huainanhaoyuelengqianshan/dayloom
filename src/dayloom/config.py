"""Configuration: TOML on disk, validated into typed models.

Design notes
------------
* The file is optional; every field has a default, so ``dayloom start`` works
  on a fresh machine without any setup.
* Unknown keys are an error on purpose: a silently ignored typo in
  ``blacklist_titles`` is a privacy bug, not a cosmetic one.
* Only ``DAYLOOM_HOME`` is read from the environment. Secrets belong in the
  TOML file (chmod/ACL'd to the user), not in shell profiles that leak into
  every child process.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import paths

CaptureMode = Literal["active_window", "full_monitor", "all_monitors"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralConfig(_Section):
    data_dir: str = ""
    day_boundary_hour: int = Field(default=4, ge=0, le=23)


class CaptureConfig(_Section):
    enabled: bool = True
    probe_interval: float = Field(default=2.0, ge=0.5, le=60.0)
    heartbeat: float = Field(default=20.0, ge=2.0, le=600.0)
    mode: CaptureMode = "full_monitor"
    max_edge: int = Field(default=1280, ge=320, le=4096)
    webp_quality: int = Field(default=72, ge=10, le=100)
    dedupe_distance: int = Field(default=3, ge=0, le=32)
    idle_seconds: int = Field(default=300, ge=30)
    #: Watching a video produces no keyboard input, so "idle" is a bad signal
    #: there. Media windows get this much longer leash before capture stops.
    media_idle_seconds: int = Field(default=1800, ge=300, le=21600)
    max_still_minutes: int = Field(default=10, ge=0, le=240)
    blacklist_titles: list[str] = Field(
        default_factory=lambda: [
            r"(?i)1password",
            r"(?i)bitwarden",
            r"(?i)keepass",
            r"(?i)lastpass",
        ]
    )
    blacklist_apps: list[str] = Field(default_factory=lambda: ["keepass.exe", "1password.exe"])

    @field_validator("blacklist_titles")
    @classmethod
    def _titles_compile(cls, value: list[str]) -> list[str]:
        import re

        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:  # pragma: no cover - config level guard
                raise ValueError(f"blacklist_titles 里的正则不合法: {pattern!r} ({exc})") from exc
        return value


class StorageConfig(_Section):
    keep_frames_days: int = Field(default=14, ge=0, le=3650)


class QualityGate(_Section):
    """When to escalate a batch to the next model tier."""

    min_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    max_unknown_ratio: float = Field(default=0.35, ge=0.0, le=1.0)
    max_shallow_ratio: float = Field(default=0.6, ge=0.0, le=1.0)


class LLMTier(_Section):
    """An escalation target: usually a bigger/cloud model."""

    name: str = ""
    base_url: str
    model: str
    api_key: str = ""
    max_frames: int = Field(default=12, ge=1, le=64)
    timeout: float = Field(default=120.0, ge=5.0, le=1800.0)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)

    @field_validator("base_url")
    @classmethod
    def _url_ok(cls, value: str) -> str:
        v = value.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("llm.base_url 必须以 http:// 或 https:// 开头")
        return v

    def to_llm(self) -> LLMConfig:
        return LLMConfig(
            enabled=True,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            timeout=self.timeout,
            max_frames=self.max_frames,
            temperature=self.temperature,
        )


class LLMConfig(_Section):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = ""
    model: str = "qwen2.5vl:7b"
    timeout: float = Field(default=120.0, ge=5.0, le=1800.0)
    max_frames: int = Field(default=12, ge=1, le=64)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    #: One extra small image per batch: the enlarged taskbar strips of the
    #: selected frames, so the model can see which icon was highlighted (the
    #: "you are here" cue a human reads at a glance). Costs little; turn it off
    #: for very small local models whose context is already tight.
    focus_strip: bool = True
    quality: QualityGate = Field(default_factory=QualityGate)
    tiers: list[LLMTier] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def _url_ok(cls, value: str) -> str:
        v = value.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("llm.base_url 必须以 http:// 或 https:// 开头")
        return v

    @property
    def configured(self) -> bool:
        return bool(self.model.strip()) and bool(self.base_url.strip())

    def tier_chain(self) -> list[tuple[str, LLMConfig]]:
        """Primary first, then escalation targets in order."""
        primary = LLMConfig(
            enabled=self.enabled,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            timeout=self.timeout,
            max_frames=self.max_frames,
            temperature=self.temperature,
            focus_strip=self.focus_strip,
            quality=self.quality,
        )
        chain: list[tuple[str, LLMConfig]] = [("primary", primary)]
        for index, tier in enumerate(self.tiers, start=1):
            name = tier.name.strip() or f"tier{index}"
            chain.append((name, tier.to_llm()))
        return chain

    def masked(self) -> dict[str, object]:
        """Safe to log or print: never echo the key."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": "***" if self.api_key else "",
            "max_frames": self.max_frames,
            "tiers": [{"name": t.name, "model": t.model} for t in self.tiers],
        }


class AnalysisConfig(_Section):
    batch_minutes: int = Field(default=15, ge=1, le=120)
    grace_seconds: int = Field(default=120, ge=0, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    session_gap_seconds: int = Field(default=120, ge=0, le=3600)
    #: Hard daily cap on images sent to the model. Images dominate token spend;
    #: 0 means unlimited. The daemon stops analyzing (and says so) when hit.
    max_images_per_day: int = Field(default=0, ge=0, le=100000)


class Config(_Section):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)

    # -- derived ---------------------------------------------------------
    @property
    def home(self) -> Path:
        raw = self.general.data_dir.strip()
        if raw:
            p = Path(raw).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
        return paths.home()

    @property
    def db_path(self) -> Path:
        return paths.db_file(self.home)

    @property
    def frames_root(self) -> Path:
        return paths.frames_dir(self.home)

    @property
    def batch_seconds(self) -> float:
        return float(self.analysis.batch_minutes * 60)

    def reload(self) -> Config:
        return load(self.home / "config.toml")


def load(path: Path | None = None) -> Config:
    """Load config from ``path`` (default: <home>/config.toml). Missing file -> defaults."""
    raw = paths.home() / "config.toml" if path is None else Path(path)
    if not raw.exists():
        return Config()
    with raw.open("rb") as fh:
        data = tomllib.load(fh)
    return Config.model_validate(data)


def resolve_path(path: Path | None = None) -> Path:
    return paths.home() / "config.toml" if path is None else Path(path)


def with_api_model(cfg: Config, *, model: str, base_url: str, api_key: str) -> Config:
    """Point the primary tier at a cloud OpenAI-compatible endpoint.

    Existing escalation tiers are left alone: switching the primary should not
    silently rewrite the rest of the chain.
    """
    new = cfg.model_copy(deep=True)
    new.llm.base_url = base_url.rstrip("/")
    new.llm.model = model
    new.llm.api_key = api_key
    new.llm.enabled = True
    # Cloud responses come back in seconds; don't inherit a 15-minute local
    # timeout, and don't inherit the local frame count either.
    new.llm.timeout = 180.0
    new.llm.max_frames = max(6, min(new.llm.max_frames, 12))
    return Config.model_validate(new.model_dump())


def with_local_model(cfg: Config, model: str, base_url: str) -> Config:
    """Switch the primary tier to a local endpoint, keeping the old one as a fallback.

    The previous cloud model becomes an escalation tier instead of being thrown
    away: a local 3B model is cheap, but it is not always good, and the quality
    gate can then decide when the cloud model is worth paying for.
    """
    new = cfg.model_copy(deep=True)
    previous = new.llm
    target = base_url.rstrip("/")

    # Drop any tier that points at the same endpoint we are switching to, so
    # running this twice does not stack duplicate tiers.
    tiers = [t for t in previous.tiers if t.base_url.rstrip("/") != target]
    if previous.configured and previous.base_url.rstrip("/") != target:
        tiers.insert(
            0,
            LLMTier(
                name="cloud",
                base_url=previous.base_url,
                api_key=previous.api_key,
                model=previous.model,
                max_frames=previous.max_frames,
                timeout=previous.timeout,
            ),
        )

    new.llm.base_url = base_url
    new.llm.api_key = ""
    new.llm.model = model
    new.llm.enabled = True
    # A 3B model on CPU needs minutes, not seconds: 4 frames ≈ 4k prompt tokens
    # at ~35 tok/s prefill, and the default 120s timeout cancelled every request
    # halfway through. Fewer frames also keeps the prompt small.
    new.llm.max_frames = min(previous.max_frames, 4)
    new.llm.timeout = max(previous.timeout, 900.0)
    new.llm.tiers = tiers
    return Config.model_validate(new.model_dump())


def write_config(cfg: Config, path: Path | None = None) -> Path:
    """Write the effective config back to TOML, keeping a backup of the old file.

    Comments are *not* preserved: this is the format the settings UI produces.
    If you maintain a hand-commented ``config.toml``, edit it by hand — the app
    only rewrites the file when you press "保存并应用", and always leaves a
    ``.bak`` next to it.
    """
    import tomli_w

    # Re-run the validators: a bad regex written to disk would stop the daemon
    # from starting, and the user would see "why is nothing recording?".
    cfg = Config.model_validate(cfg.model_dump())

    target = resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.with_suffix(target.suffix + ".bak").write_bytes(target.read_bytes())
    with target.open("wb") as fh:
        tomli_w.dump(cfg.model_dump(), fh)
    return target


def dump_example() -> str:
    """A commented example when available, otherwise a generated one.

    The repository ships a hand-written, commented ``config.example.toml``; a
    frozen build does not have it on disk, so fall back to serialising the
    defaults — always in sync, just without the prose.
    """
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    if example.exists():
        return example.read_text(encoding="utf-8")
    import tomli_w

    header = "# dayloom 配置（由默认值生成；带注释的版本见仓库里的 config.example.toml）\n"
    return header + tomli_w.dumps(Config().model_dump())
