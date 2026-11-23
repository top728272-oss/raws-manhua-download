"""Configuration loading/saving for Manhua Raws Downloader.

Reads/writes `config.yaml`. Provides a typed-ish accessor via dataclasses so the
rest of the app doesn't pass raw dicts around. No CLI - the GUI is the interface,
so this is the single source of truth for tunables.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "output_dir": "./downloads",
    "default_formats": ["folder"],
    "keep_webp": True,
    "convert_to": "jpg",
    "download": {
        "max_concurrent": 5,
        "batch_size": 5,
        "retries": 3,
        "backoff_seconds": 2,
        "fallback_enabled": True,
        "fallback_min_similarity": 0.7,
        "skip_existing": True,
        "recovery_attempts": 3,
    },
    "network": {
        "proxy": "",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "request_delay_ms": 400,
    },
    "cache": {
        "max_size_mb": 512,
        "prune_on_startup": True,
    },
    "sources": {
        "guazimanhua": {
            "enabled": True,
            "base_url": "https://www.guazimanhua.com",
        },
        "comicbox": {
            "enabled": True,
            "base_url": "https://www.comicbox.xyz",
            "backup_urls": ["https://www.wu55comic.world"],
        },
        "jjmhw9": {
            "enabled": True,
            "base_url": "https://www.jjmhw9.top",
        },
        "san3004": {
            "enabled": True,
            "base_url": "https://www.3004ss.com",
            "backup_urls": ["https://m.3004aa.cc"],
        },
        "manhuagui": {
            "enabled": True,
            "base_url": "https://www.manhuagui.com",
            "image_host": "https://eu1.hamreus.com",
        },
        "mkzhan": {
            "enabled": True,
            "base_url": "https://www.mkzhan.com",
            "api_base": "https://comic.mkzcdn.com",
            "quality": 3,
            "include_vip": False,
        },
        "copymanga": {
            "enabled": True,
            "base_url": "https://www.mangacopy.com",
            "group": "default",
            "page_order": "words",
        },
        "hipmh": {
            "enabled": True,
            "base_url": "https://m.hipmh.com",
            "reader_base": "https://reader.hipmh.top",
            "api_base": "https://hipapi1.s3file.top",
            "img_base": "https://hip-tx-1.s3imgs.top",
            "cover_base": "https://cover.s3imgs.top",
            "chapter_pages": 40,
        },
    },
    "logging": {
        "level": "INFO",
        "file": "manhua-dl.log",
    },
}


@dataclass
class DownloadConfig:
    max_concurrent: int = 5
    batch_size: int = 5
    retries: int = 3
    backoff_seconds: int = 2

    fallback_enabled: bool = True
    fallback_min_similarity: float = 0.7

    skip_existing: bool = True

    recovery_attempts: int = 3


@dataclass
class NetworkConfig:
    proxy: str = ""
    user_agent: str = ""
    request_delay_ms: int = 400


@dataclass
class CacheConfig:
    """Bounds for the on-disk cover cache (`.cache/covers`)."""

    max_size_mb: int = 512
    prune_on_startup: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "manhua-dl.log"


@dataclass
class AppConfig:
    output_dir: str = "./downloads"
    default_formats: list[str] = field(default_factory=lambda: ["folder", "cbz"])
    keep_webp: bool = True
    convert_to: str = "jpg"
    download: DownloadConfig = field(default_factory=DownloadConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def output_path(self) -> Path:
        return (PROJECT_ROOT / self.output_dir).resolve()

    def source_settings(self, source_id: str) -> dict[str, Any]:
        return self.sources.get(source_id, {})


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path | str | None = None) -> AppConfig:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:
            logger.warning("Failed to parse %s (%s); using defaults.", cfg_path, exc)
            data = {}
    else:
        logger.info("No config at %s; using built-in defaults.", cfg_path)

    merged = _deep_merge(_DEFAULTS, data)
    network_data = dict(merged["network"])
    return AppConfig(
        output_dir=merged["output_dir"],
        default_formats=list(merged["default_formats"]),
        keep_webp=bool(merged["keep_webp"]),
        convert_to=merged["convert_to"],
        download=DownloadConfig(**merged["download"]),
        network=NetworkConfig(**network_data),
        cache=CacheConfig(**merged["cache"]),
        sources=merged["sources"],
        logging=LoggingConfig(**merged["logging"]),
        raw=merged,
    )


def setup_logging(cfg: AppConfig) -> None:
    level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    log_file = PROJECT_ROOT / cfg.logging.file
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logger.debug("Logging initialized at level %s -> %s", cfg.logging.level, log_file)
