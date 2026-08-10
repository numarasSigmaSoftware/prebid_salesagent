"""Configuration management for Prebid Sales Agent.

Provides Pydantic-based configuration classes for type-safe, validated configuration
management using environment variables.
"""

import logging
import os
import re
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_WEBHOOK_AUDIT_HMAC_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,32}")

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})


def _env_flag_is_true(name: str) -> bool:
    """Parse a boolean-ish env var against an explicit truthy vocabulary (case-
    insensitive, surrounding whitespace ignored).

    ``bool(os.environ.get(name))`` treats ANY non-empty string as true --
    including "false", "0", "no", "off" -- which is exactly backwards for a flag
    an operator set specifically to turn something OFF. A value not in the
    truthy vocabulary (including unset, "", or an unrecognized string) is false.
    """
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


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
        WebhookDeliveryLog.webhook_url column (see redact_webhook_url_for_audit), so an
        unrestricted value -- colons, angle brackets, newlines, control characters,
        unbounded length -- could corrupt the audit identifier's structure or open a
        log-injection vector. It's operator-set deployment config, not buyer input,
        but a copy-paste error (e.g. pasting a whole secret-manager blob into the
        wrong env var) shouldn't be able to do either of those things.

        Uses fullmatch(), not match(): re's `$` anchor matches immediately before
        a FINAL trailing newline (not just at the true end of string), so
        `PATTERN.match(v)` with a `^...$` pattern would accept "v1\n" -- the exact
        newline-injection payload this validator exists to reject. fullmatch()
        requires the match to consume the entire string, which a trailing
        character outside the allowed class (the newline) prevents.
        """
        if not _WEBHOOK_AUDIT_HMAC_KEY_ID_PATTERN.fullmatch(v):
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

        # Enforced only where production was DECLARED (ENVIRONMENT=production), and
        # merely warned where it is newly INFERRED (PRODUCTION / FLY_APP_NAME).
        #
        # is_production() was broadened in this change so the check reaches Fly.io
        # deployments that never set ENVIRONMENT. Raising on that newly-inferred set
        # would stop a downstream service from booting on upgrade, for a configuration
        # that was valid the day before and that its operator never changed -- and in
        # an open-source project those deployments cannot be surveyed or warned ahead
        # of time. So the newly-captured set gets a loud, actionable warning now and
        # enforcement in a later release; the declared set is enforced immediately,
        # because that contract was always "this is production, hold me to it".
        stripped_hmac_key = config.webhook_audit_hmac_key.strip()
        hmac_problem: str | None = None
        if not stripped_hmac_key:
            hmac_problem = (
                "WEBHOOK_AUDIT_HMAC_KEY is not set -- it keys the HMAC that redacts "
                "buyer-supplied webhook URLs before they reach logs and "
                "WebhookDeliveryLog; an unset (or whitespace-only) key leaves those "
                "audit identifiers matchable offline against guessed URLs."
            )
        elif len(stripped_hmac_key) < MIN_WEBHOOK_AUDIT_HMAC_KEY_LENGTH:
            hmac_problem = (
                f"WEBHOOK_AUDIT_HMAC_KEY is shorter than the required {MIN_WEBHOOK_AUDIT_HMAC_KEY_LENGTH} characters."
            )

        if hmac_problem:
            if declares_production_explicitly():
                raise ValueError(f"{hmac_problem} This is required in production.")
            if is_production():
                logger.warning(
                    "%s This deployment is treated as production because PRODUCTION or "
                    "FLY_APP_NAME is set, even though ENVIRONMENT is not 'production'. "
                    "Set WEBHOOK_AUDIT_HMAC_KEY to a value of at least %d characters -- "
                    "a future release will make this fatal.",
                    hmac_problem,
                    MIN_WEBHOOK_AUDIT_HMAC_KEY_LENGTH,
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

    True if any recognized production signal is present. scripts/run_server.py,
    src/core/auth.py, src/core/logging_config.py, and src/core/audit_logger.py
    used to each open-code their own ``FLY_APP_NAME or PRODUCTION`` bare-presence
    check instead of calling this function -- a bare presence check cannot see
    that PRODUCTION=false means "explicitly off" (it reads as truthy, matching
    a non-empty string), and never consulted ENVIRONMENT at all. All four now
    call this function directly, closing the same truthy-vocabulary bug
    ``_env_flag_is_true`` already fixes below, so an operator who sets
    PRODUCTION=false to turn it off is honoured everywhere, not just here. The
    convergence is pinned by
    TestProductionSignalConverged.test_no_site_still_open_codes_the_bare_presence_check
    (tests/unit/test_db_config.py) so a future edit cannot silently reintroduce
    an open-coded copy at any of them.

    A deployment that sets PRODUCTION or relies on Fly.io's
    auto-populated FLY_APP_NAME, but never explicitly sets ENVIRONMENT=production,
    used to read as non-production here even though scripts/run_server.py already
    bound it to 0.0.0.0 as production traffic -- silently skipping every
    production-only check gated on this function (e.g. WEBHOOK_AUDIT_HMAC_KEY
    strength in validate_configuration()).

    PRODUCTION is parsed against an explicit truthy vocabulary (_env_flag_is_true),
    not bare presence -- an operator setting PRODUCTION=false to explicitly turn
    it off must not flip this to True. FLY_APP_NAME is a presence check, not a
    boolean flag: Fly.io populates it with the actual app name, so any non-empty
    value already means "running on Fly.io."

    Returns:
        bool: True if ENVIRONMENT=production, or PRODUCTION is truthy, or
            FLY_APP_NAME is set (Fly.io sets this automatically on every deploy).
    """
    return declares_production_explicitly() or (_env_flag_is_true("PRODUCTION") or bool(os.environ.get("FLY_APP_NAME")))


def declares_production_explicitly() -> bool:
    """True only for ENVIRONMENT=production -- the pre-existing production contract.

    Separated from :func:`is_production` so a check can distinguish a deployment that
    DECLARED itself production from one this codebase newly INFERS is production.

    That distinction matters because this is an open-source project: broadening
    is_production() reclassifies deployments that changed nothing, and a check that
    hard-fails on the newly-inferred set would stop a downstream service from booting
    on upgrade -- for a configuration that was correct the day before. Tightening the
    contract for operators who explicitly set ENVIRONMENT=production is fair warning;
    doing it to a Fly.io deployment that merely has FLY_APP_NAME populated is not.

    Enforce on this predicate; WARN on the difference between it and is_production().

    What the broadening actually reaches — the complete set, because a partial list
    is what makes the reclassification look smaller than it is. Every behavior gated
    on is_production() changes for a deployment that has FLY_APP_NAME or a truthy
    PRODUCTION but never set ENVIRONMENT:

    - ``webhook_validator._strict_mode`` — SSRF policy. HTTPS becomes REQUIRED and the
      testing localhost bypass is withdrawn. This is the one with teeth: a Fly-only
      deployment that delivered webhooks over plain HTTP now has them REJECTED.
    - ``get_pydantic_extra_mode`` — forbid to ignore, so unknown request fields are
      accepted instead of rejected.
    - ``mcp_compat_middleware`` (two sites) — unknown fields are stripped silently, and
      a validation failure is retried with a deep strip.
    - ``product_conversion`` — a product missing delivery_measurement takes the adapter
      default with an info log rather than the non-production path.
    - ``validate_configuration`` — the webhook-audit HMAC key requirement, which is
      warned rather than enforced for exactly this newly-inferred set (above).

    Note the directions differ: the SSRF change is a tightening that can break a
    working deployment, while the extra-mode and compat changes are loosenings. A
    reader who only saw the loosenings would misjudge the upgrade risk.

    ``scripts/run_server.py``, ``auth.py``, ``logging_config.py``, and
    ``audit_logger.py`` also call ``is_production()`` (verbose-auth-log
    suppression, structured-vs-basic logging format, and the audit console
    handler), but NOT for the broadening this section documents -- for a
    Fly-only or PRODUCTION-truthy deployment they already agreed with
    is_production() before converging onto it (both were True). Their actual
    behavior change is on the other axis, PRODUCTION=false and
    ENVIRONMENT=production-alone, documented on :func:`is_production` itself.
    Listed here only so the completeness scan above finds every caller.
    """
    return os.getenv("ENVIRONMENT", "development").lower() == "production"


def get_pydantic_extra_mode() -> Literal["ignore", "forbid"]:
    """Get Pydantic extra field handling mode based on environment.

    Production: "ignore" - Accept extra fields for forward compatibility
    Non-production: "forbid" - Reject extra fields to catch bugs early
    """
    return "ignore" if is_production() else "forbid"
