from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from proseforge.runtime.profile import RuntimeProfile, validate_profile_database


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROSEFORGE_",
        extra="ignore",
        case_sensitive=False,
    )

    # PROSEFORGE_ENV is kept as a legacy alias: older compose files and docs
    # used it, and extra="ignore" silently dropped it, which disabled the
    # production security validation below.
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("PROSEFORGE_ENVIRONMENT", "PROSEFORGE_ENV", "environment"),
    )
    public_url: str = "http://localhost:3000"
    database_url: str = "postgresql+asyncpg://proseforge:proseforge@postgres:5432/proseforge"
    sync_database_url: str = ""
    redis_url: str = "redis://redis:6379/0"
    blob_root: str = "/data/blobs"
    backup_root: str = "/data/backups"
    master_key: SecretStr = SecretStr("replace-with-32-byte-base64-key")
    jwt_secret: SecretStr = SecretStr("replace-with-long-random-secret")
    bootstrap_admin_email: str = "admin@example.local"
    bootstrap_admin_password: SecretStr = SecretStr("change-me-now")
    max_upload_bytes: int = 50 * 1024 * 1024
    allowed_local_provider_hosts: tuple[str, ...] = Field(default_factory=tuple)

    runtime_profile: RuntimeProfile = RuntimeProfile.SERVER
    data_dir: str | None = None
    frontend_dir: str | None = None
    skills_dir: str = "packs/skills"  # 内置 skill 目录（相对 CWD；voice packs 同款默认机制）
    # 角色人格文件目录：默认解析为包安装位置下的 packs/personas（与
    # embedding_cache_dir 同款 default_factory 惰性解析），native 安装
    # CWD 非项目根时也能命中；PROSEFORGE_PERSONAS_DIR 仍可覆盖。
    personas_dir: str = Field(
        default_factory=lambda: str(Path(__file__).resolve().parent.parent / "packs" / "personas")
    )
    host: str = "127.0.0.1"
    port: int = 8000
    serve_web: bool = False
    native_queue_poll_seconds: float = 1.0
    native_worker_concurrency: int = 2
    agent_rate_limit_read_per_minute: int = 60
    agent_rate_limit_write_per_minute: int = 20
    # Failed-attempt rate limit for /api/v1/auth/* (PROSEFORGE_AUTH_RATE_LIMIT_PER_MINUTE);
    # successful logins and idempotent setup probes are not counted.
    auth_rate_limit_per_minute: int = 10
    # Session JWT lifetime in minutes (PROSEFORGE_SESSION_TOKEN_MINUTES); the
    # auth cookie max_age is derived from this value.
    session_token_minutes: int = 1440
    # Open multi-user registration (PROSEFORGE_ALLOW_REGISTRATION). Off by
    # default: a personal instance stays single-owner (only the one-time
    # /auth/setup). Enable on shared/demo instances so visitors can
    # self-register USER accounts via /api/v1/auth/register.
    allow_registration: bool = False

    # Narrative RAG local embedding engine (fastembed/ONNX, see
    # infrastructure/embeddings/local.py). hf_endpoint deliberately reads
    # HF_ENDPOINT (no PROSEFORGE_ prefix): it is the variable huggingface_hub
    # itself honors (verified on 1.24: HF_HUB_ENDPOINT does NOT flip
    # constants.ENDPOINT, HF_ENDPOINT does), and we re-export it pre-import.
    # Default cache dir is the repo-bundled packaging/models/ so an offline
    # distribution works out of the box; PROSEFORGE_EMBEDDING_CACHE_DIR
    # still overrides. default_factory keeps the path lazy (never resolved
    # at import time).
    embedding_cache_dir: str = Field(
        default_factory=lambda: str(Path(__file__).resolve().parent.parent / "packaging" / "models")
    )
    hf_endpoint: str = Field(
        default="https://hf-mirror.com",
        validation_alias=AliasChoices("HF_ENDPOINT", "HF_HUB_ENDPOINT", "hf_endpoint"),
    )
    local_embedding_threads: int = 2

    # Built-in web search tool (fence contract + worker-side search rounds).
    # Failover order: first success wins, so trailing engines cost nothing.
    search_engines: list[str] = Field(default_factory=lambda: ["bing", "duckduckgo", "google", "yahoo", "brave", "mojeek", "ecosia", "startpage", "baidu"])
    search_max_results: int = 5
    search_fetch_max_chars: int = 6000
    search_timeout_seconds: float = 10.0
    search_max_rounds: int = 2
    # Server-side intent fallback: when the user switch is on and the current
    # user message matches one of these patterns, the worker runs one
    # proactive search and injects the results as a system block (does not
    # count toward search_max_rounds).
    search_auto_intent_enabled: bool = True
    search_auto_intent_patterns: list[str] = Field(default_factory=lambda: [
        "今天", "昨天", "最新", "最近", "近期", "新闻", "现在", "实时", "当前",
        "价格", "股价", "天气", "汇率", "几号", "本周", "这周", "这个月", "今年",
        "2025", "2026",
        "today", "yesterday", "latest", "recent", "news", "current", "price",
        "stock", "weather", "now",
    ])

    # Tool system phase 1: unified tool fence rounds + web reader tools.
    max_tool_rounds: int = 4
    tool_result_max_chars: int = 8000
    webtools_timeout_seconds: float = 20.0
    webtools_cache_ttl_seconds: int = 600

    # run_code sandbox (bubblewrap; see infrastructure/sandbox/runner.py).
    code_exec_timeout_seconds: int = 60
    code_exec_max_timeout_seconds: int = 120
    sandbox_venv_path: str = "/opt/proseforge/sandbox-venv"
    code_exec_max_output_chars: int = 64000
    code_exec_max_files: int = 5
    code_exec_max_file_bytes: int = 10 * 1024 * 1024

    # License system (PROSEFORGE_LICENSE_CENTER_URL etc.). Empty center URL
    # disables licensing entirely — the default for development and
    # self-hosted instances, so every licensing check becomes a no-op.
    license_center_url: str | None = None
    # Hex-encoded Ed25519 public key of the license center; used to verify
    # certificate signatures on enroll/handshake responses.
    license_center_public_key: str | None = None
    # Hours a verified certificate stays trusted after the last successful
    # handshake (measured on the monotonic clock, never the system clock).
    license_grace_hours: int = 24

    # Single-user concurrent agent-run cap (PENDING/RUNNING count). Default 3
    # protects API quota; raise via PROSEFORGE_MAX_ACTIVE_RUNS_PER_USER on
    # hosts with headroom.
    max_active_runs_per_user: int = 3

    # promise_verify RAG 定向取证开关（PROSEFORGE_PROMISE_RAG_VERIFY）。
    # 开：每条 due 承诺走「承诺→段落」RAG 定向取证 + 本章直查分段扫描，
    # 取证为空/索引异常显式回落全章通读旧路径并落审计事件；关：保持现状
    # 全章通读。默认 off——修正 6 归因纪律要求基线②分两次采样
    # （金字塔 only → +取证），默认关保证首发行为与基线①一致。
    promise_rag_verify: bool = False

    @model_validator(mode="after")
    def validate_runtime(self) -> Settings:
        validate_profile_database(self.runtime_profile, self.database_url)
        return self

    @model_validator(mode="after")
    def validate_security(self) -> Settings:
        if self.environment.lower() not in {"production", "prod"}:
            return self

        placeholders = {
            "replace-with-32-byte-base64-key",
            "replace-with-long-random-secret",
            "change-me-now",
        }
        if self.master_key.get_secret_value() in placeholders:
            raise ValueError("PROSEFORGE_MASTER_KEY must be replaced in production")
        if self.jwt_secret.get_secret_value() in placeholders:
            raise ValueError("PROSEFORGE_JWT_SECRET must be replaced in production")
        if len(self.jwt_secret.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("PROSEFORGE_JWT_SECRET must be at least 32 bytes")
        try:
            decoded = base64.b64decode(self.master_key.get_secret_value(), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("PROSEFORGE_MASTER_KEY must be valid base64") from exc
        if len(decoded) != 32:
            raise ValueError("PROSEFORGE_MASTER_KEY must decode to 32 bytes")
        for name, value in (("blob_root", self.blob_root), ("backup_root", self.backup_root)):
            if not Path(value).is_absolute():
                raise ValueError(f"{name} must be absolute in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
