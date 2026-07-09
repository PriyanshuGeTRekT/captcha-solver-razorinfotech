import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


DEFAULT_CONFIG_DIR = Path.home() / ".captcha-solver"
DEFAULT_PROFILES_DIR = DEFAULT_CONFIG_DIR / "profiles"
DEFAULT_MODELS_DIR = DEFAULT_CONFIG_DIR / "models"


class ProxyConfig(BaseModel):
    server: str = ""
    username: Optional[str] = None
    password: Optional[str] = None

    def as_playwright(self) -> dict | None:
        if not self.server:
            return None
        cfg: dict = {"server": self.server}
        if self.username:
            cfg["username"] = self.username
        if self.password:
            cfg["password"] = self.password
        return cfg


class FingerprintConfig(BaseModel):
    randomized: bool = True
    canvas_noise: bool = True
    webgl_vendor: str = "Intel Inc."
    webgl_renderer: str = "Intel Iris OpenGL Engine"
    platform: str = "Win32"
    timezone: str = "America/New_York"
    language: str = "en-US"
    viewport_width: int = 1920
    viewport_height: int = 1080


class BrowserProfile(BaseModel):
    name: str
    user_data_dir: str = ""
    proxy: Optional[ProxyConfig] = None
    fingerprint: FingerprintConfig = Field(default_factory=FingerprintConfig)
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0
    success_count: int = 0


class SolverConfig(BaseModel):
    browser_headless: bool = False
    browser_timeout_ms: int = 30000
    audio_model_size: str = "base.en"
    audio_compute_type: str = "int8"
    clip_model_name: str = "ViT-B-32"
    max_retries: int = 3
    retry_delay_ms: int = 2000
    profiles_dir: str = str(DEFAULT_PROFILES_DIR)
    models_dir: str = str(DEFAULT_MODELS_DIR)

    @field_validator("max_retries")
    @classmethod
    def validate_max_retries(cls, v):
        if v < 1 or v > 20:
            raise ValueError("max_retries must be between 1 and 20")
        return v

    @field_validator("retry_delay_ms")
    @classmethod
    def validate_retry_delay(cls, v):
        if v < 100 or v > 60000:
            raise ValueError("retry_delay_ms must be between 100 and 60000")
        return v

    @field_validator("browser_timeout_ms")
    @classmethod
    def validate_timeout(cls, v):
        if v < 5000 or v > 120000:
            raise ValueError("browser_timeout_ms must be between 5000 and 120000")
        return v

    @field_validator("audio_model_size")
    @classmethod
    def validate_audio_model(cls, v):
        valid = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en", "large-v2", "large-v3"}
        if v not in valid:
            raise ValueError(f"audio_model_size must be one of {valid}")
        return v


class AppConfig(BaseModel):
    solver: SolverConfig = Field(default_factory=SolverConfig)
    profiles: list[BrowserProfile] = []

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        path = path or DEFAULT_CONFIG_DIR / "config.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return cls.model_validate(data)
        return cls()

    def save(self, path: Optional[Path] = None) -> None:
        path = path or DEFAULT_CONFIG_DIR / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.model_dump(), f, indent=2, default=str)

    def get_profile(self, name: str) -> Optional[BrowserProfile]:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def add_profile(self, profile: BrowserProfile) -> None:
        existing = self.get_profile(profile.name)
        if existing:
            self.profiles.remove(existing)
        self.profiles.append(profile)


_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig.load()
    return _config


def save_config() -> None:
    if _config:
        _config.save()
