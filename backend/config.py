"""Typed application configuration with explicit project-local defaults."""
from __future__ import annotations

import os
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

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


def _is_loopback_host(value: str) -> bool:
    normalized = value.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_loopback_url(value: str) -> bool:
    """True only for `http://` URLs pointing back at this machine.

    The redirect URI is where the identity provider sends the browser back to, so production must
    use HTTPS. Plain HTTP is tolerated for loopback addresses only, which keeps a developer able to
    exercise the login flow without provisioning a certificate.
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    return parts.scheme == "http" and bool(parts.hostname) and _is_loopback_host(parts.hostname)


class AppSettings(BaseModel):
    """All non-secret runtime configuration used by the query application."""

    model_config = ConfigDict(frozen=True)

    project_root: Path = Field(default=PROJECT_ROOT, exclude=True)
    app_name: str = "DocMind IT Assistant"
    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8020
    admin_host: str = "127.0.0.1"
    admin_port: int = 8021
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
    admin_index_path: Path = PROJECT_ROOT / "web" / "admin.html"
    artifact_output_path: Path = PROJECT_ROOT / "data" / "artifacts"
    model_mode: Literal["knowledge", "local", "cloud"] = "knowledge"
    local_provider: str = "ollama"
    local_model: str = ""
    cloud_provider: str = "qwen"
    cloud_model: str = ""
    builtin_model: str = "deterministic"
    cloud_base_url: str = ""
    local_base_url: str = ""
    custom_base_url: str = ""
    model_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    model_max_retries: int = Field(default=1, ge=0, le=3)
    model_retry_backoff_seconds: float = Field(default=0.25, ge=0.0, le=10.0)
    model_max_output_tokens: int = Field(default=512, ge=1, le=32768)
    model_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    embedding_mode: Literal["hash", "provider"] = "hash"
    embedding_provider: str = "qwen"
    embedding_model: str = "text-embedding-v3"
    embedding_base_url: str = ""
    embedding_dimension: int = 1024
    embedding_timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    document_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    document_max_characters: int = Field(default=2_000_000, ge=1000, le=20_000_000)
    document_max_pages: int = Field(default=500, ge=1, le=5000)
    chunk_max_chars: int = Field(default=1200, ge=200, le=8000)
    chunk_overlap_chars: int = Field(default=150, ge=0, le=2000)
    retrieval_top_k: int = Field(default=5, ge=1, le=20)
    rerank_enabled: bool = True
    rerank_mode: Literal["lexical", "api"] = "lexical"
    rerank_base_url: str = ""
    rerank_model: str = ""
    rerank_api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    rerank_top_n: int = Field(default=20, ge=1, le=100)
    chunk_child_max_chars: int = Field(default=400, ge=80, le=2000)
    governance_mode: Literal["direct", "review"] = "direct"
    governance_require_separation_of_duties: bool = True
    governance_allow_admin_override: bool = False
    ingestion_worker_enabled: bool = False
    ingestion_engine: Literal["simple", "langgraph"] = "simple"
    ingestion_checkpoint_path: Path = PROJECT_ROOT / "data" / "worker-checkpoints.db"
    ingestion_worker_id: str = ""
    ingestion_poll_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)
    ingestion_backoff_max_seconds: int = Field(default=1800, ge=0, le=86400)
    ingestion_job_timeout_seconds: int = Field(default=600, ge=30, le=86400)
    slow_request_ms: int = Field(default=1000, ge=0, le=60000)
    slow_db_ms: int = Field(default=200, ge=0, le=60000)
    query_daily_quota: int = Field(default=1000, ge=0, le=100000)
    retention_days: int = Field(default=365, ge=1, le=3650)
    retention_grace_days: int = Field(default=30, ge=0, le=3650)
    ingestion_heartbeat_seconds: int = Field(default=30, ge=5, le=3600)
    evaluation_gate_mode: Literal["off", "warn", "block"] = "warn"
    evaluation_allow_override: bool = False
    evaluation_min_recall: float = Field(default=0.8, ge=0.0, le=1.0)
    evaluation_min_citation_accuracy: float = Field(default=0.9, ge=0.0, le=1.0)
    evaluation_max_regression: float = Field(default=0.05, ge=0.0, le=1.0)
    evaluation_top_k: int = Field(default=5, ge=1, le=20)
    evaluation_faithfulness_enabled: bool = True
    evaluation_min_faithfulness: float = Field(default=0.7, ge=0.0, le=1.0)
    auth_mode: Literal["development", "trusted_headers", "oidc", "local"] = "development"
    local_username: str = "admin"
    local_password_hash: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    local_display_name: str = "本地管理员"
    local_roles: str = "admin,auditor,viewer"
    local_session_hours: int = Field(default=12, ge=1, le=168)
    guest_login_enabled: bool = False
    guest_session_hours: int = Field(default=2, ge=1, le=24)
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_role_claim: str = "roles"
    oidc_group_claim: str = "groups"
    oidc_leeway_seconds: int = Field(default=30, ge=0, le=300)
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_session_hours: int = Field(default=12, ge=1, le=168)
    oidc_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    # Optional endpoint overrides. When left empty the endpoints are read from the issuer's
    # `/.well-known/openid-configuration`, which is what makes swapping identity providers a
    # configuration-only change. They exist for on-premise providers that do not publish a
    # discovery document.
    oidc_authorization_endpoint: str = ""
    oidc_token_endpoint: str = ""
    oidc_end_session_url: str = ""
    auth_subject_salt: SecretStr = Field(
        default=SecretStr("development-only"), exclude=True, repr=False,
    )
    query_field_key: SecretStr = Field(
        default=SecretStr(""), exclude=True, repr=False,
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True
    configured_credentials: frozenset[str] = Field(
        default_factory=frozenset, exclude=True, repr=False,
    )
    credentials: dict[str, SecretStr] = Field(default_factory=dict, exclude=True, repr=False)

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("IT_PORT 必须在 1 到 65535 之间")
        return value

    @field_validator("admin_port")
    @classmethod
    def validate_admin_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("IT_ADMIN_PORT 必须在 1 到 65535 之间")
        return value

    @field_validator("host", "admin_host")
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
        if self.environment == "production" and self.embedding_mode != "provider":
            raise ValueError("生产环境 IT_EMBEDDING_MODE 必须使用 provider")
        if self.embedding_dimension != 1024:
            raise ValueError("IT_EMBEDDING_DIMENSION 必须为当前索引维度 1024")
        if self.chunk_overlap_chars >= self.chunk_max_chars:
            raise ValueError("IT_CHUNK_OVERLAP_CHARS 必须小于 IT_CHUNK_MAX_CHARS")
        if self.ingestion_heartbeat_seconds >= self.ingestion_job_timeout_seconds:
            # Otherwise the reclaim sweep would consider a healthy, still-working job abandoned.
            raise ValueError(
                "IT_INGESTION_HEARTBEAT_SECONDS 必须小于 IT_INGESTION_JOB_TIMEOUT_SECONDS"
            )
        if self.auth_mode == "development" and not (
            _is_loopback_host(self.host) and _is_loopback_host(self.admin_host)
        ):
            raise ValueError("development 认证只能监听本机回环地址")
        if self.auth_mode == "local" and (
            not self.local_username or not self.local_password_hash.get_secret_value()
        ):
            raise ValueError("local 认证必须配置 IT_LOCAL_USERNAME 和 IT_LOCAL_PASSWORD_HASH")
        if self.environment == "production" and self.auth_mode != "oidc":
            raise ValueError("生产环境 IT_AUTH_MODE 必须使用 oidc")
        if self.auth_mode == "oidc":
            if not self.oidc_issuer.startswith("https://"):
                raise ValueError("OIDC 签发方必须使用 HTTPS")
            if not self.oidc_audience:
                raise ValueError("IT_OIDC_AUDIENCE 不能为空")
            # Optional: when it is absent the discovery document supplies the JWKS URL, which is
            # what lets an operator point the service at a new provider by changing the issuer
            # alone. It is only validated when it is actually set.
            if self.oidc_jwks_url and not self.oidc_jwks_url.startswith("https://"):
                raise ValueError("OIDC JWKS 地址必须使用 HTTPS")
            if not self.auth_subject_salt.get_secret_value():
                raise ValueError("IT_AUTH_SUBJECT_SALT 不能为空")
            if not self.oidc_client_id:
                raise ValueError("IT_OIDC_CLIENT_ID 不能为空")
            if not self.oidc_redirect_uri:
                raise ValueError("IT_OIDC_REDIRECT_URI 不能为空")
            if not self.oidc_redirect_uri.startswith("https://") and not (
                self.environment != "production" and _is_loopback_url(self.oidc_redirect_uri)
            ):
                raise ValueError("OIDC 回调地址必须使用 HTTPS")
            if "openid" not in self.oidc_scope_list:
                raise ValueError("IT_OIDC_SCOPES 必须包含 openid")
        if self.environment == "production" and len(self.auth_subject_salt.get_secret_value()) < 32:
            raise ValueError("生产环境 IT_AUTH_SUBJECT_SALT 至少需要 32 个字符")
        if self.environment == "production" and not self.query_field_key.get_secret_value():
            raise ValueError(
                "生产环境 IT_QUERY_FIELD_KEY 必须配置（用于 query.question 字段级加密）"
            )
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

        credential_values = {
            provider.api_key_env: SecretStr(read(provider.api_key_env, "").strip())
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
            admin_host=read("IT_ADMIN_HOST", "127.0.0.1"),
            admin_port=int(read("IT_ADMIN_PORT", "8021")),
            database_path=database_path,
            database_url=read("IT_DATABASE_URL", default_database_url).strip(),
            database_pool_size=int(read("IT_DB_POOL_SIZE", "5")),
            database_max_overflow=int(read("IT_DB_MAX_OVERFLOW", "10")),
            database_pool_timeout=int(read("IT_DB_POOL_TIMEOUT", "30")),
            database_connect_timeout=int(read("IT_DB_CONNECT_TIMEOUT", "5")),
            knowledge_path=path_value("IT_KNOWLEDGE_PATH", Path("knowledge.md")),
            web_index_path=path_value("IT_WEB_INDEX_PATH", Path("web/index.html")),
            admin_index_path=path_value("IT_ADMIN_INDEX_PATH", Path("web/admin.html")),
            artifact_output_path=path_value("IT_ARTIFACT_OUTPUT_PATH", Path("data/artifacts")),
            model_mode=read("IT_MODEL_MODE", "knowledge").strip().lower(),
            local_provider=read("IT_LOCAL_PROVIDER", "ollama").strip().lower(),
            local_model=read("IT_LOCAL_MODEL", "").strip(),
            cloud_provider=read("IT_CLOUD_PROVIDER", "qwen").strip().lower(),
            cloud_model=read("IT_CLOUD_MODEL", "").strip(),
            builtin_model=read("IT_BUILTIN_MODEL", "deterministic").strip(),
            cloud_base_url=read("IT_CLOUD_BASE_URL", "").strip(),
            local_base_url=read("IT_LOCAL_BASE_URL", "").strip(),
            custom_base_url=read("IT_CUSTOM_BASE_URL", "").strip(),
            model_timeout_seconds=float(read("IT_MODEL_TIMEOUT_SECONDS", "30")),
            model_max_retries=int(read("IT_MODEL_MAX_RETRIES", "1")),
            model_retry_backoff_seconds=float(read("IT_MODEL_RETRY_BACKOFF_SECONDS", "0.25")),
            model_max_output_tokens=int(read("IT_MODEL_MAX_OUTPUT_TOKENS", "512")),
            model_temperature=float(read("IT_MODEL_TEMPERATURE", "0.1")),
            embedding_mode=read("IT_EMBEDDING_MODE", "hash").strip().lower(),
            embedding_provider=read("IT_EMBEDDING_PROVIDER", "qwen").strip().lower(),
            embedding_model=read("IT_EMBEDDING_MODEL", "text-embedding-v3").strip(),
            embedding_base_url=read("IT_EMBEDDING_BASE_URL", "").strip(),
            embedding_dimension=int(read("IT_EMBEDDING_DIMENSION", "1024")),
            embedding_timeout_seconds=float(read("IT_EMBEDDING_TIMEOUT_SECONDS", "60")),
            embedding_batch_size=int(read("IT_EMBEDDING_BATCH_SIZE", "16")),
            document_max_bytes=int(read("IT_DOCUMENT_MAX_BYTES", str(20 * 1024 * 1024))),
            document_max_characters=int(read("IT_DOCUMENT_MAX_CHARACTERS", "2000000")),
            document_max_pages=int(read("IT_DOCUMENT_MAX_PAGES", "500")),
            chunk_max_chars=int(read("IT_CHUNK_MAX_CHARS", "1200")),
            chunk_overlap_chars=int(read("IT_CHUNK_OVERLAP_CHARS", "150")),
            retrieval_top_k=int(read("IT_RETRIEVAL_TOP_K", "5")),
            rerank_enabled=_bool(read("IT_RERANK_ENABLED", "true")),
            rerank_mode=read("IT_RERANK_MODE", "lexical").strip().lower(),
            rerank_base_url=read("IT_RERANK_BASE_URL", "").strip(),
            rerank_model=read("IT_RERANK_MODEL", "").strip(),
            rerank_api_key=SecretStr(read("IT_RERANK_API_KEY", "").strip()),
            rerank_top_n=int(read("IT_RERANK_TOP_N", "20")),
            chunk_child_max_chars=int(read("IT_CHUNK_CHILD_MAX_CHARS", "400")),
            governance_mode=read("IT_GOVERNANCE_MODE", "direct").strip().lower(),
            governance_require_separation_of_duties=_bool(
                read("IT_GOVERNANCE_REQUIRE_SEPARATION_OF_DUTIES", "true"),
            ),
            governance_allow_admin_override=_bool(
                read("IT_GOVERNANCE_ALLOW_ADMIN_OVERRIDE", "false"),
            ),
            ingestion_worker_enabled=_bool(read("IT_INGESTION_WORKER_ENABLED", "false")),
            ingestion_engine=read("IT_INGESTION_ENGINE", "simple").strip().lower(),
            ingestion_checkpoint_path=path_value(
                "IT_INGESTION_CHECKPOINT_PATH", Path("data/worker-checkpoints.db"),
            ),
            ingestion_worker_id=read("IT_INGESTION_WORKER_ID", "").strip(),
            ingestion_poll_seconds=float(read("IT_INGESTION_POLL_SECONDS", "2")),
            ingestion_max_attempts=int(read("IT_INGESTION_MAX_ATTEMPTS", "3")),
            ingestion_backoff_max_seconds=int(read("IT_INGESTION_BACKOFF_MAX_SECONDS", "1800")),
            slow_request_ms=int(read("IT_SLOW_REQUEST_MS", "1000")),
            slow_db_ms=int(read("IT_SLOW_DB_MS", "200")),
            query_daily_quota=int(read("IT_QUERY_DAILY_QUOTA", "1000")),
            retention_days=int(read("IT_RETENTION_DAYS", "365")),
            retention_grace_days=int(read("IT_RETENTION_GRACE_DAYS", "30")),
            ingestion_job_timeout_seconds=int(read("IT_INGESTION_JOB_TIMEOUT_SECONDS", "600")),
            ingestion_heartbeat_seconds=int(read("IT_INGESTION_HEARTBEAT_SECONDS", "30")),
            evaluation_gate_mode=read("IT_EVAL_GATE_MODE", "warn").strip().lower(),
            evaluation_allow_override=_bool(read("IT_EVAL_ALLOW_OVERRIDE", "false")),
            evaluation_min_recall=float(read("IT_EVAL_MIN_RECALL", "0.8")),
            evaluation_min_citation_accuracy=float(
                read("IT_EVAL_MIN_CITATION_ACCURACY", "0.9"),
            ),
            evaluation_max_regression=float(read("IT_EVAL_MAX_REGRESSION", "0.05")),
            evaluation_top_k=int(read("IT_EVAL_TOP_K", "5")),
            evaluation_faithfulness_enabled=_bool(read("IT_EVAL_FAITHFULNESS_ENABLED", "true")),
            evaluation_min_faithfulness=float(read("IT_EVAL_MIN_FAITHFULNESS", "0.7")),
            auth_mode=read("IT_AUTH_MODE", "development").strip().lower(),
            local_username=read("IT_LOCAL_USERNAME", "admin").strip(),
            local_password_hash=SecretStr(read("IT_LOCAL_PASSWORD_HASH", "").strip()),
            local_display_name=read("IT_LOCAL_DISPLAY_NAME", "本地管理员").strip(),
            local_roles=read("IT_LOCAL_ROLES", "admin,auditor,viewer").strip(),
            local_session_hours=int(read("IT_LOCAL_SESSION_HOURS", "12")),
            guest_login_enabled=_bool(read("IT_GUEST_LOGIN_ENABLED", "false")),
            guest_session_hours=int(read("IT_GUEST_SESSION_HOURS", "2")),
            oidc_issuer=read("IT_OIDC_ISSUER", "").strip(),
            oidc_audience=read("IT_OIDC_AUDIENCE", "").strip(),
            oidc_jwks_url=read("IT_OIDC_JWKS_URL", "").strip(),
            oidc_role_claim=read("IT_OIDC_ROLE_CLAIM", "roles").strip(),
            oidc_group_claim=read("IT_OIDC_GROUP_CLAIM", "groups").strip(),
            oidc_leeway_seconds=int(read("IT_OIDC_LEEWAY_SECONDS", "30")),
            oidc_client_id=read("IT_OIDC_CLIENT_ID", "").strip(),
            oidc_client_secret=SecretStr(read("IT_OIDC_CLIENT_SECRET", "").strip()),
            oidc_redirect_uri=read("IT_OIDC_REDIRECT_URI", "").strip(),
            oidc_scopes=read("IT_OIDC_SCOPES", "openid profile email").strip(),
            oidc_session_hours=int(read("IT_OIDC_SESSION_HOURS", "12")),
            oidc_timeout_seconds=float(read("IT_OIDC_TIMEOUT_SECONDS", "10")),
            oidc_authorization_endpoint=read("IT_OIDC_AUTHORIZATION_ENDPOINT", "").strip(),
            oidc_token_endpoint=read("IT_OIDC_TOKEN_ENDPOINT", "").strip(),
            oidc_end_session_url=read("IT_OIDC_END_SESSION_URL", "").strip(),
            auth_subject_salt=SecretStr(read("IT_AUTH_SUBJECT_SALT", "development-only")),
            query_field_key=SecretStr(read("IT_QUERY_FIELD_KEY", "").strip()),
            log_level=read("IT_LOG_LEVEL", "INFO").strip().upper(),
            log_json=_bool(read("IT_LOG_JSON", "true")),
            configured_credentials=frozenset(credential_values),
            credentials=credential_values,
        )

    @property
    def local_role_list(self) -> tuple[str, ...]:
        """Local-login roles in stable order; unknown values are ignored by the authenticator."""
        return tuple(
            part.strip().lower() for part in self.local_roles.split(",") if part.strip()
        )

    @property
    def oidc_scope_list(self) -> tuple[str, ...]:
        """Requested scopes in stable order. `openid` is what makes the response an OIDC one."""
        return tuple(
            part.strip() for part in self.oidc_scopes.replace(",", " ").split() if part.strip()
        )

    def credential_is_configured(self, name: str) -> bool:
        """Return credential presence without retaining or exposing its value."""
        return bool(name and name in self.configured_credentials)

    def credential_value(self, name: str) -> str:
        value = self.credentials.get(name)
        return value.get_secret_value() if value else ""

    def public(self) -> dict:
        """Safe status view. Secret values are deliberately absent from this model."""
        return {
            "app_name": self.app_name,
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "admin_host": self.admin_host,
            "admin_port": self.admin_port,
            "model_mode": self.model_mode,
            "governance_mode": self.governance_mode,
            "governance_require_separation_of_duties": self.governance_require_separation_of_duties,
            "governance_allow_admin_override": self.governance_allow_admin_override,
            "ingestion_worker_enabled": self.ingestion_worker_enabled,
            "ingestion_engine": self.ingestion_engine,
            "ingestion_poll_seconds": self.ingestion_poll_seconds,
            "ingestion_max_attempts": self.ingestion_max_attempts,
            "ingestion_backoff_max_seconds": self.ingestion_backoff_max_seconds,
            "ingestion_job_timeout_seconds": self.ingestion_job_timeout_seconds,
            "slow_request_ms": self.slow_request_ms,
            "slow_db_ms": self.slow_db_ms,
            "rerank_enabled": self.rerank_enabled,
            "rerank_mode": self.rerank_mode,
            "rerank_base_url": self.rerank_base_url,
            "rerank_model": self.rerank_model,
            "rerank_top_n": self.rerank_top_n,
            "chunk_child_max_chars": self.chunk_child_max_chars,
            "query_daily_quota": self.query_daily_quota,
            "retention_days": self.retention_days,
            "retention_grace_days": self.retention_grace_days,
            "evaluation_gate_mode": self.evaluation_gate_mode,
            "evaluation_allow_override": self.evaluation_allow_override,
            "evaluation_min_recall": self.evaluation_min_recall,
            "evaluation_min_citation_accuracy": self.evaluation_min_citation_accuracy,
            "evaluation_max_regression": self.evaluation_max_regression,
            "evaluation_faithfulness_enabled": self.evaluation_faithfulness_enabled,
            "evaluation_min_faithfulness": self.evaluation_min_faithfulness,
            "auth_mode": self.auth_mode,
            "log_level": self.log_level,
            "log_json": self.log_json,
        }
