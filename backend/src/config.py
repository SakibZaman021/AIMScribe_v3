"""
AIMScribe AI Backend - Configuration Management
Using Pydantic Settings for environment-based configuration.
"""

import os
from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # ================================================================
    # Azure OpenAI - Transcription (Audio Transcriptions API)
    # ================================================================
    azure_transcribe_endpoint: str = Field(
        default="",
        description="Azure OpenAI endpoint for transcription"
    )
    azure_transcribe_api_key: str = Field(
        default="",
        description="Azure OpenAI API key for transcription"
    )
    azure_transcribe_deployment: str = Field(
        default="gpt-4o-transcribe-diarize",
        description="Azure OpenAI deployment name for transcription"
    )
    azure_transcribe_api_version: str = Field(
        default="2025-03-01-preview",
        description="Azure OpenAI API version for transcription"
    )
    
    # ================================================================
    # Azure OpenAI - NER & Agents
    # ================================================================
    azure_ner_endpoint: str = Field(
        default="",
        description="Azure OpenAI endpoint for NER"
    )
    azure_ner_api_key: str = Field(
        default="",
        description="Azure OpenAI API key for NER"
    )
    azure_ner_deployment: str = Field(
        default="gpt-5.2-chat",
        description="Azure OpenAI deployment name for NER"
    )
    azure_api_version: str = Field(
        default="2024-02-15-preview",
        description="Azure OpenAI API version"
    )
    
    # ================================================================
    # PostgreSQL Database
    # ================================================================
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="aimscribe_db")
    postgres_user: str = Field(default="aimscribe_user")
    postgres_password: str = Field(default="")
    postgres_sslmode: str = Field(default="prefer")
    postgres_pool_min: int = Field(
        default=2,
        description="Minimum connections in pool"
    )
    postgres_pool_max: int = Field(
        default=10,
        description="Maximum connections in pool"
    )
    
    @property
    def database_url(self) -> str:
        """Generate PostgreSQL connection URL."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
    
    # ================================================================
    # Server Configuration
    # ================================================================
    server_port: int = Field(default=6000)
    server_host: str = Field(default="0.0.0.0")
    log_level: str = Field(default="INFO")
    debug: bool = Field(default=False)
    
    # ================================================================
    # Prompts Configuration
    # ================================================================
    prompts_dir: str = Field(
        default="src/prompts",
        description="Directory containing prompt templates"
    )
    
    # ================================================================
    # MinIO Configuration
    # ================================================================
    minio_endpoint: str = Field(default="minio:9000")
    minio_external_endpoint: str = Field(
        default="localhost:9000",
        description="External MinIO endpoint for presigned URLs (accessible from clients)"
    )
    minio_access_key: str = Field(default="aimscribe")
    minio_secret_key: str = Field(default="aimscribe123")
    minio_bucket: str = Field(default="aimscribe-audio")
    minio_secure: bool = Field(default=False)
    # Signing region. Cloudflare R2 requires "auto"; MinIO accepts anything
    # provided it matches. It is part of the SigV4 signature, so a mismatch
    # fails every presigned request with SignatureDoesNotMatch.
    minio_region: str = Field(default="us-east-1")
    
    # ================================================================
    # Redis Configuration
    # ================================================================
    redis_host: str = Field(default="redis")
    redis_port: int = Field(default=6379)
    redis_db: int = Field(default=0)
    redis_password: Optional[str] = Field(default=None)
    redis_ssl: bool = Field(default=False, description="Use SSL for Redis (required for Upstash)")

    # ================================================================
    # Processing Configuration
    # ================================================================
    ner_trigger_clips: int = Field(
        default=2,
        description="Number of clips before triggering NER extraction"
    )
    worker_concurrency: int = Field(default=1)
    
    # ================================================================
    # Storage
    # ================================================================
    input_folder: str = Field(default="/app/audio")  # Legacy support
    chroma_folder: str = Field(default="/app/chromadb")
    
    # ================================================================
    # Ngrok (Optional)
    # ================================================================
    ngrok_authtoken: Optional[str] = Field(default=None)
    ngrok_domain: Optional[str] = Field(default=None)
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.
    
    Using lru_cache ensures settings are loaded once and reused.
    """
    return Settings()


# Convenience function for direct access
settings = get_settings()
