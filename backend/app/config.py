from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_name: str = "Customer Service Platform"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    database_url: str = (
        "postgresql+asyncpg://customer_service:customer_service@localhost:5432/customer_service"
    )
    redis_url: str = "redis://localhost:6379/0"
    event_backend: Literal["redis", "memory"] = "redis"
    minio_enabled: bool = True
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_bucket: str = "customer-service"
    minio_secure: bool = False
    agent_mode: Literal["mock", "live"] = "mock"
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "kimi-k2.6"
    agent_timeout_seconds: float = 45.0
    agent_max_turns: int = 6
    agent_max_tool_calls: int = 8
    agent_history_messages: int = 12
    agent_history_max_tokens: int = Field(default=2_560, ge=128)
    agent_current_message_max_tokens: int = Field(default=4_096, ge=128)
    supervisor_plan_max_tokens: int = Field(default=1_024, ge=64)
    supervisor_response_max_tokens: int = Field(default=1_024, ge=64)
    conversation_memory_ttl_seconds: int = Field(default=86_400, ge=60)
    tool_timeout_seconds: float = 8.0
    order_backend: Literal["mock", "postgres", "http"] = "postgres"
    order_oms_base_url: str = ""
    order_oms_api_key: str = ""
    order_oms_timeout_seconds: float = 5.0
    after_sales_upload_max_bytes: int = 8_000_000
    after_sales_video_upload_max_bytes: int = 50_000_000
    after_sales_max_evidence_files: int = 6
    after_sales_admin_token: str = ""
    after_sales_worker_poll_seconds: float = 1.0
    after_sales_outbox_max_attempts: int = 5
    evidence_analysis_enabled: bool = True
    evidence_analysis_timeout_seconds: float = 90.0
    evidence_analysis_prompt_version: str = "evidence-precheck-v1"
    embedding_mode: Literal["bge", "hash", "openai"] = "bge"
    embedding_dimensions: int = 512
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_batch_size: int = 32
    embedding_warmup_on_startup: bool = True
    knowledge_chunk_chars: int = 900
    knowledge_chunk_overlap: int = 120
    knowledge_upload_max_bytes: int = 2_000_000
    knowledge_admin_token: str = ""
    knowledge_bm25_k1: float = 1.5
    knowledge_bm25_b: float = 0.75
    knowledge_rrf_k: int = 60
    knowledge_retrieval_candidates: int = 20
    trace_path: str = "artifacts/safe-trace.ndjson"
    auto_create_schema: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
