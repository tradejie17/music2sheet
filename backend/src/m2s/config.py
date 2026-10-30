"""Runtime settings (env prefix M2S_, e.g. M2S_TRANSKUN_DEVICE=mps)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="M2S_")

    data_dir: Path = REPO_ROOT / "data"
    transkun_device: str = "mps"  # spike: identical output, ~1.37x faster, 3.65GB
    beat_device: str = "cpu"
    mscore_path: Path = Path("/Applications/MuseScore 4.app/Contents/MacOS/mscore")
    max_audio_s: float = 15 * 60
    # "svsep" refines hands with piano_svsep when installed (scripts/setup_svsep.sh)
    hand_model: str = "svsep"
    engine_rss_limit_gb: float = 9.0

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "m2s.sqlite3"


@lru_cache
def settings() -> Settings:
    return Settings()
