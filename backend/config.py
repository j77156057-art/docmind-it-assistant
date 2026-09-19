"""Typed application configuration with explicit project-local defaults."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .providers import PROVIDERS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_env_file(path: Path) -> dict[str, str]:
    """Read a small dotenv subset without mutating the process environment."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"非法布尔配置：{value}")


class AppSettings(BaseModel):
    """All non-secret runtime configuration used by the query application."""

    model_config = ConfigDict(frozen=True)

    project_root: Path = Field(default=PROJECT_ROOT, exclude=True)
    app_name: str = "DocMind IT Assistant"
    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8020
    database_path: Path = PROJECT_ROOT / "data" / "queries.db"
    database_url: str = Field(
        default=f"sqlite:///{(PROJECT_ROOT / 'data' / 'queries.db').as_posix()}",
        exclude=True,
        repr=False,
    )
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=200)
    database_pool_timeout: int = Field(default=30, ge=1, le=300)
    database_connect_timeout: int = Field(default=5, ge=1, le=60)
    knowledge_path: Path = PROJECT_ROOT / "knowledge.md"
    web_index_path: Path = PROJECT_ROOT / "web" / "index.html"
    model_mode: Literal["knowledge", "local", "cloud"] = "knowledge"
    local_provider: str = "ollama"
    local_model: str = ""
    cloud_provider: str = "qwen"
    cloud_model: str = ""
    builtin_model: str = "deterministic"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True
    configured_credentials: frozenset[str] = Field(
        default_factory=frozenset, exclude=True, repr=False,
    )

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("IT_PORT 必须在 1 到 65535 之间")
        return value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("IT_HOST 不能为空或包含空格")
        return value

    @model_validator(mode="after")
    def require_postgresql_in_production(self) -> "AppSettings":
        if self.environment == "production" and not self.database_url.startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("生产环境 IT_DATABASE_URL 必须使用 PostgreSQL")
        return self

    @classmethod
    def from_environment(cls, project_root: Path | str | None = None) -> "AppSettings":
        root = Path(project_root or PROJECT_ROOT).resolve()
        file_values = _read_env_file(root / ".env")

        def read(name: str, default: str) -> str:
            return os.environ.get(name, file_values.get(name, default))

        def path_value(name: str, default: Path) -> Path:
            value = Path(read(name, str(default))).expanduser()
            return value.resolve() if value.is_absolute() else (root / value).resolve()

        credential_names = {
            provider.api_key_env
            for provider in PROVIDERS.values()
            if provider.api_key_env and read(provider.api_key_env, "").strip()
        }
        database_path = path_value("IT_DATABASE_PATH", Path("data/queries.db"))
        default_database_url = f"sqlite:///{database_path.as_posix()}"

        return cls(
            project_root=root,
            app_name=read("IT_APP_NAME", "DocMind IT Assistant"),
            environment=read("IT_ENVIRONMENT", "development").strip().lower(),
            host=read("IT_HOST", "127.0.0.1"),
            port=int(read("IT_PORT", "8020")),
            database_path=database_path,
            database_url=read("IT_DATABASE_URL", default_database_url).strip(),
            database_pool_size=int(read("IT_DB_POOL_SIZE", "5")),
            database_max_overflow=int(read("IT_DB_MAX_OVERFLOW", "10")),
            database_pool_timeout=int(read("IT_DB_POOL_TIMEOUT", "30")),
            database_connect_timeout=int(read("IT_DB_CONNECT_TIMEOUT", "5")),
            knowledge_path=path_value("IT_KNOWLEDGE_PATH", Path("knowledge.md")),
            web_index_path=path_value("IT_WEB_INDEX_PATH", Path("web/index.html")),
            model_mode=read("IT_MODEL_MODE", "knowledge").strip().lower(),
            local_provider=read("IT_LOCAL_PROVIDER", "ollama").strip().lower(),
            local_model=read("IT_LOCAL_MODEL", "").strip(),
            cloud_provider=read("IT_CLOUD_PROVIDER", "qwen").strip().lower(),
            cloud_model=read("IT_CLOUD_MODEL", "").strip(),
            builtin_model=read("IT_BUILTIN_MODEL", "deterministic").strip(),
            log_level=read("IT_LOG_LEVEL", "INFO").strip().upper(),
            log_json=_bool(read("IT_LOG_JSON", "true")),
            configured_credentials=frozenset(credential_names),
        )

    def credential_is_configured(self, name: str) -> bool:
        """Return credential presence without retaining or exposing its value."""
        return bool(name and name in self.configured_credentials)

    def public(self) -> dict:
        """Safe status view. Secret values are deliberately absent from this model."""
        return {
            "app_name": self.app_name,
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "model_mode": self.model_mode,
            "log_level": self.log_level,
            "log_json": self.log_json,
        }
