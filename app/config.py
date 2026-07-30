from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import os
import yaml

from app.voice_planner.scoring import ScoringConfig


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


@dataclass
class PathSettings:
    books_dir: Path
    output_dir: Path
    temp_dir: Path
    log_dir: Path


@dataclass
class KokoroSettings:
    api_url: str
    api_key: str
    model: str
    voice: str
    speed: float
    timeout_seconds: float
    retry_delays_seconds: list[float] = field(default_factory=lambda: [2.0, 5.0, 10.0])


@dataclass
class ConversionSettings:
    chunk_chars: int
    chapter_filename_format: str
    m4b_bitrate: str
    parallel_workers: int
    cleanup_temp_on_success: bool
    preserve_failed_temp: bool
    resume_on_startup: bool


@dataclass
class AnalysisSettings:
    llm_provider: str
    ollama_url: str
    ollama_model: str
    analysis_chunk_size: int
    cache_enabled: bool
    cache_directory: str


@dataclass
class NormalizationSettings:
    enabled: bool
    output_dir_name: str
    deduplicate_dialogue: bool
    minimum_confidence: float
    rejection_labels: dict[str, list[str]] = field(default_factory=dict)
    aliases: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass
class VoicePlannerSettings:
    schema_version: int
    enabled: bool
    registry_path: str
    registry_dir_name: str
    series_dir_name: str
    books_dir_name: str
    voice_plan_filename: str
    report_filename: str
    preserve_user_edits: bool
    dry_run_default: bool
    scoring: ScoringConfig = field(default_factory=ScoringConfig.default)


@dataclass
class StoryforgeSettings:
    paths: PathSettings
    kokoro: KokoroSettings
    conversion: ConversionSettings
    analysis: AnalysisSettings
    normalization: NormalizationSettings
    voice_planner: VoicePlannerSettings


def load_settings(config_path: str | Path | None = None) -> StoryforgeSettings:
    path = Path(config_path).expanduser() if config_path else Path(os.getenv("STORYFORGE_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    voice_planner_raw = data.get("voice_planner") or {}
    scoring_raw = voice_planner_raw.get("scoring") or {}
    normalization_raw = data.get("normalization") or {}
    rejection_labels = {
        "characters": list((normalization_raw.get("rejection_labels") or {}).get("characters", [])),
        "places": list((normalization_raw.get("rejection_labels") or {}).get("places", [])),
        "organizations": list((normalization_raw.get("rejection_labels") or {}).get("organizations", [])),
    }
    aliases_raw = normalization_raw.get("aliases") or {}
    aliases = {
        "characters": {str(k): list(v) for k, v in (aliases_raw.get("characters") or {}).items()},
        "places": {str(k): list(v) for k, v in (aliases_raw.get("places") or {}).items()},
        "organizations": {str(k): list(v) for k, v in (aliases_raw.get("organizations") or {}).items()},
    }

    return StoryforgeSettings(
        paths=PathSettings(
            books_dir=_as_path(data, ("paths", "books_dir")),
            output_dir=_as_path(data, ("paths", "output_dir")),
            temp_dir=_as_path(data, ("paths", "temp_dir")),
            log_dir=_as_path(data, ("paths", "log_dir")),
        ),
        kokoro=KokoroSettings(
            api_url=_as_str(data, ("kokoro", "api_url")),
            api_key=_as_str(data, ("kokoro", "api_key"), default="not-needed"),
            model=_as_str(data, ("kokoro", "model"), default="kokoro"),
            voice=_as_str(data, ("kokoro", "voice"), default="af_heart"),
            speed=float(_as_number(data, ("kokoro", "speed"), default=1.0)),
            timeout_seconds=float(_as_number(data, ("kokoro", "timeout_seconds"), default=120.0)),
            retry_delays_seconds=[float(v) for v in _as_sequence(data, ("kokoro", "retry_delays_seconds"), default=[2, 5, 10])],
        ),
        conversion=ConversionSettings(
            chunk_chars=int(_as_number(data, ("conversion", "chunk_chars"), default=1200)),
            chapter_filename_format=_as_str(data, ("conversion", "chapter_filename_format"), default="Chapter {chapter:03d}.wav"),
            m4b_bitrate=_as_str(data, ("conversion", "m4b_bitrate"), default="128k"),
            parallel_workers=int(_as_number(data, ("conversion", "parallel_workers"), default=1)),
            cleanup_temp_on_success=bool(_as_bool(data, ("conversion", "cleanup_temp_on_success"), default=True)),
            preserve_failed_temp=bool(_as_bool(data, ("conversion", "preserve_failed_temp"), default=True)),
            resume_on_startup=bool(_as_bool(data, ("conversion", "resume_on_startup"), default=True)),
        ),
        analysis=AnalysisSettings(
            llm_provider=_as_str(data, ("analysis", "llm_provider"), default="ollama"),
            ollama_url=_as_str(data, ("analysis", "ollama_url"), default="http://127.0.0.1:11434"),
            ollama_model=_as_str(data, ("analysis", "ollama_model"), default="llama3.1"),
            analysis_chunk_size=int(_as_number(data, ("analysis", "analysis_chunk_size"), default=6000)),
            cache_enabled=bool(_as_bool(data, ("analysis", "cache_enabled"), default=True)),
            cache_directory=_as_str(data, ("analysis", "cache_directory"), default="analysis"),
        ),
        normalization=NormalizationSettings(
            enabled=bool(_as_bool(normalization_raw, ("enabled",), default=True)),
            output_dir_name=str(normalization_raw.get("output_dir_name", "analysis_normalized")),
            deduplicate_dialogue=bool(_as_bool(normalization_raw, ("deduplicate_dialogue",), default=True)),
            minimum_confidence=float(normalization_raw.get("minimum_confidence", 0.5)),
            rejection_labels=rejection_labels,
            aliases=aliases,
        ),
        voice_planner=VoicePlannerSettings(
            schema_version=int(_as_number(data, ("voice_planner", "schema_version"), default=1)),
            enabled=bool(_as_bool(data, ("voice_planner", "enabled"), default=True)),
            registry_path=_as_str(data, ("voice_planner", "registry_path"), default="voices/registry.json"),
            registry_dir_name=_as_str(data, ("voice_planner", "registry_dir_name"), default="voices"),
            series_dir_name=_as_str(data, ("voice_planner", "series_dir_name"), default="series"),
            books_dir_name=_as_str(data, ("voice_planner", "books_dir_name"), default="books"),
            voice_plan_filename=_as_str(data, ("voice_planner", "voice_plan_filename"), default="voice_plan.json"),
            report_filename=_as_str(data, ("voice_planner", "report_filename"), default="voice_assignment_report.json"),
            preserve_user_edits=bool(_as_bool(data, ("voice_planner", "preserve_user_edits"), default=True)),
            dry_run_default=bool(_as_bool(data, ("voice_planner", "dry_run_default"), default=True)),
            scoring=ScoringConfig.from_mapping(scoring_raw),
        ),
    )


def _lookup(data: dict[str, Any], keys: Sequence[str]) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _as_path(data: dict[str, Any], keys: Sequence[str]) -> Path:
    value = _lookup(data, keys)
    if value is None:
        raise KeyError(f"Missing config value: {'.'.join(keys)}")
    return Path(str(value)).expanduser()


def _as_str(data: dict[str, Any], keys: Sequence[str], default: str | None = None) -> str:
    value = _lookup(data, keys)
    if value is None:
        if default is None:
            raise KeyError(f"Missing config value: {'.'.join(keys)}")
        return default
    return str(value)


def _as_number(data: dict[str, Any], keys: Sequence[str], default: float | int | None = None) -> float | int:
    value = _lookup(data, keys)
    if value is None:
        if default is None:
            raise KeyError(f"Missing config value: {'.'.join(keys)}")
        return default
    return value


def _as_bool(data: dict[str, Any], keys: Sequence[str], default: bool | None = None) -> bool:
    value = _lookup(data, keys)
    if value is None:
        if default is None:
            raise KeyError(f"Missing config value: {'.'.join(keys)}")
        return default
    return bool(value)


def _as_sequence(data: dict[str, Any], keys: Sequence[str], default: Sequence[Any] | None = None) -> Sequence[Any]:
    value = _lookup(data, keys)
    if value is None:
        if default is None:
            raise KeyError(f"Missing config value: {'.'.join(keys)}")
        return default
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"Expected sequence for config value: {'.'.join(keys)}")
    return value
