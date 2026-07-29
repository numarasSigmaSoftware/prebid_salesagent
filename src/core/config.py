"""Configuration management for Prebid Sales Agent.

Provides Pydantic-based configuration classes for type-safe, validated configuration
management using environment variables.
"""

import os
import re
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEBHOOK_AUDIT_HMAC_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


class GAMOAuthConfig(BaseSettings):
    """Google Ad Manager OAuth configuration."""

    client_id: str = Field(default="", description="GAM OAuth Client ID from Google Cloud Console")
    client_secret: str = Field(default="", description="GAM OAuth Client Secret from Google Cloud Console")

    model_config = SettingsConfigDict(env_prefix="GAM_OAUTH_", case_sensitive=False)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, v):
        """Validate GAM OAuth Client ID format (only if provided)."""
        if not v:
            return v  # Allow empty - validation happens when GAM adapter is used
        if not v.endswith(".apps.googleusercontent.com"):
            raise ValueError("GAM OAuth Client ID must end with '.apps.googleusercontent.com'")
        return v

    @field_validator("client_secret")
    @classmethod
    def validate_client_secret(cls, v):
        """Validate GAM OAuth Client Secret format (only if provided)."""
        if not v:
            return v  # Allow empty - validation happens when GAM adapter is used
        if not v.startswith("GOCSPX-"):
            raise ValueError("GAM OAuth Client Secret must start with 'GOCSPX-'")
        return v


class DatabaseConfig(BaseSettings):
    """Database configuration."""

    url: str | None = Field(default=None, description="Database connection URL")
    type: str = Field(default="postgresql", description="Database type")

    model_config = SettingsConfigDict(env_prefix="DATABASE_", case_sensitive=False)


class ServerConfig(BaseSettings):
    """Server configuration."""

    adcp_sales_port: int = Field(default=8080, description="MCP server port")
    admin_ui_port: int = Field(default=8001, description="Admin UI port")
    a2a_port: int = Field(default=8091, description="A2A server port")

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class GoogleOAuthConfig(BaseSettings):
    """Google OAuth configuration for admin UI."""

    client_id: str | None = Field(default=None, description="Google OAuth Client ID")
    client_secret: str | None = Field(default=None, description="Google OAuth Client Secret")
    credentials_file: str | None = Field(default=None, description="Path to Google OAuth credentials file")

    model_config = SettingsConfigDict(env_prefix="GOOGLE_", case_sensitive=False)


class SuperAdminConfig(BaseSettings):
    """Super admin configuration."""

    emails: str = Field(default="", description="Comma-separated list of super admin emails")
    domains: str | None = Field(default=None, description="Comma-separated list of super admin domains")

    model_config = SettingsConfigDict(env_prefix="SUPER_ADMIN_", case_sensitive=False)

    @property
    def email_list(self) -> list[str]:
        """Get super admin emails as a list."""
        return [email.strip() for email in self.emails.split(",") if email.strip()]

    @property
    def domain_list(self) -> list[str]:
        """Get super admin domains as a list."""
        if not self.domains:
            return []
        return [domain.strip() for domain in self.domains.split(",") if domain.strip()]


class AppConfig(BaseSettings):
    """Main application configuration."""

    gemini_api_key: str | None = Field(
        default=None, description="Platform-level Gemini API key (optional - tenants can configure their own)"
    )
    flask_secret_key: str = Field(default="dev-secret-key-change-in-production", description="Flask secret key")
    webhook_audit_hmac_key: str = Field(
        default="",
        description=(
            "Server secret keying the webhook delivery-log URL-redaction HMAC "
            "(WEBHOOK_AUDIT_HMAC_KEY). A blank value is only acceptable outside "
            "production; validate_configuration() rejects it in production. Deliberately "
            "separate from flask_secret_key -- reusing a session-signing key as a durable "
            "correlation key means routine session-key rotation silently severs "
            "correlation with every historical WebhookDeliveryLog row."
        ),
    )
    webhook_audit_hmac_key_id: str = Field(
        default="v1",
        description=(
            "Identifier for the current webhook_audit_hmac_key generation "
            "(WEBHOOK_AUDIT_HMAC_KEY_ID), embedded in every redacted audit identifier. "
            "Bump this alongside a webhook_audit_hmac_key rotation so historical rows "
            "stay labeled with the key generation that produced them, instead of "
            "becoming silently unrecognizable."
        ),
    )
    debug: bool = Field(default=False, description="Enable debug mode")
    environment: str = Field(default="development", description="Environment: production, staging, or development")

    # Configuration objects
    # BaseSettings subclasses read from environment; mypy doesn't understand this pattern
    gam_oauth: GAMOAuthConfig = Field(default_factory=GAMOAuthConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    google_oauth: GoogleOAuthConfig = Field(default_factory=GoogleOAuthConfig)
    superadmin: SuperAdminConfig = Field(default_factory=SuperAdminConfig)

    @field_validator("webhook_audit_hmac_key_id")
    @classmethod
    def validate_webhook_audit_hmac_key_id(cls, v: str) -> str:
        """Restrict to a bounded, safe format.

        This value is embedded verbatim into log lines and the durable
        WebhookDeliveryLog.webhook_url column (see _redact_url_credentials), so an
        unrestricted value -- colons, angle brackets, newlines, control characters,
        unbounded length -- could corrupt the audit identifier's structure or open a
        log-injection vector. It's operator-set deployment config, not buyer input,
        but a copy-paste error (e.g. pasting a whole secret-manager blob into the
        wrong env var) shouldn't be able to do either of those things.
        """
        if not _WEBHOOK_AUDIT_HMAC_KEY_ID_PATTERN.match(v):
            raise ValueError(f"WEBHOOK_AUDIT_HMAC_KEY_ID must match ^[A-Za-z0-9._-]{{1,32}}$ (got {v!r})")
        return v

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


# Global configuration instance
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


MIN_WEBHOOK_AUDIT_HMAC_KEY_LENGTH = 32


def validate_configuration() -> None:
    """Validate all configuration at startup.

    Raises:
        ValueError: If required configuration is missing or invalid
        RuntimeError: If configuration validation fails
    """
    try:
        config = get_config()

        # Validate GAM OAuth configuration
        if config.gam_oauth:
            # Configuration validation happens automatically via Pydantic
            pass

        # Note: GEMINI_API_KEY is optional - tenants configure their own AI keys
        # Note: SUPER_ADMIN_EMAILS is optional - per-tenant OIDC with Setup Mode is the default auth flow

        if is_production():
            stripped_hmac_key = config.webhook_audit_hmac_key.strip()
            if not stripped_hmac_key:
                raise ValueError(
                    "WEBHOOK_AUDIT_HMAC_KEY must be set in production -- it keys the "
                    "HMAC that redacts buyer-supplied webhook URLs before they reach "
                    "logs and WebhookDeliveryLog; an unset (or whitespace-only) key "
                    "leaves those audit identifiers matchable offline against guessed URLs."
                )
            if len(stripped_hmac_key) < MIN_WEBHOOK_AUDIT_HMAC_KEY_LENGTH:
                raise ValueError(
                    f"WEBHOOK_AUDIT_HMAC_KEY must be at least {MIN_WEBHOOK_AUDIT_HMAC_KEY_LENGTH} "
                    "characters in production"
                )

        print("✅ Configuration validation passed")
        print(f"   GAM OAuth: {'✅ Configured' if config.gam_oauth.client_id else '❌ Not configured'}")
        print(f"   Database: {'✅ Configured' if config.database.url else '❌ Not configured'}")
        print(
            f"   Gemini API: {'✅ Configured' if config.gemini_api_key else '⚪ Not configured (tenants use own keys)'}"
        )
        print(
            f"   Super Admin: {'✅ Configured' if config.superadmin.emails else '⚪ Not configured (use per-tenant OIDC)'}"
        )

    except Exception as e:
        raise RuntimeError(f"Configuration validation failed: {str(e)}") from e


def get_gam_oauth_config() -> GAMOAuthConfig:
    """Get GAM OAuth configuration."""
    return get_config().gam_oauth


def is_production() -> bool:
    """Check if running in production environment.

    True if any recognized production signal is present -- matching the union of
    signals the real server bootstrap (scripts/run_server.py) and several src/core
    modules (auth.py, logging_config.py, audit_logger.py) already treat as
    production. A deployment that sets PRODUCTION or relies on Fly.io's
    auto-populated FLY_APP_NAME, but never explicitly sets ENVIRONMENT=production,
    used to read as non-production here even though scripts/run_server.py already
    bound it to 0.0.0.0 as production traffic -- silently skipping every
    production-only check gated on this function (e.g. WEBHOOK_AUDIT_HMAC_KEY
    strength in validate_configuration()).

    Returns:
        bool: True if ENVIRONMENT=production, or PRODUCTION is set, or FLY_APP_NAME
            is set (Fly.io sets this automatically on every deploy).
    """
    return (
        os.getenv("ENVIRONMENT", "development").lower() == "production"
        or bool(os.environ.get("PRODUCTION"))
        or bool(os.environ.get("FLY_APP_NAME"))
    )


def get_pydantic_extra_mode() -> Literal["ignore", "forbid"]:
    """Get Pydantic extra field handling mode based on environment.

    Production: "ignore" - Accept extra fields for forward compatibility
    Non-production: "forbid" - Reject extra fields to catch bugs early
    """
    return "ignore" if is_production() else "forbid"
