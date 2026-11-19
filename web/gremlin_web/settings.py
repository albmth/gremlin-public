from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # `api` serves HTTP; `worker` runs the sweep and generation processing; `local` serves HTTP
    # and runs the worker in-process, on this machine.
    role: str = "local"
    environment: str = "local"
    region: str = "eu-north-1"
    table_name: str = "gremlin-web-local-state"
    media_bucket: str = "gremlin-web-local-media"
    dynamodb_endpoint: str = ""
    s3_endpoint: str = ""
    worker_function: str = ""
    build_commit: str = ""
    # The browser-facing origin: cookies, redirects and cross-site checks use it.
    public_origin: str = "http://127.0.0.1:8080"
    monitoring_enabled: bool = False
    # The Gremlin generation API, called as one direct client.
    gremlin_base_url: str = ""
    gremlin_token: SecretStr | None = None
    gremlin_token_parameter: str = ""

    # Videos everyone shares, first come, first served, until an operator replenishes them.
    # Gremlin's own spending limits bound every GPU job beneath this.
    total_generations: int = Field(default=10, ge=0)
    address_hourly_drafts: int = Field(default=12, ge=0)
    # Videos one network address may start per UTC day, charged with the shared allowance.
    address_daily_videos: int = Field(default=3, ge=0)
    # Delivered videos are public and deleted this long after they finish. The bucket's lifecycle
    # backstop (two days, in the stack's template) must outlast this.
    retention_hours: int = Field(default=6, ge=1, le=24)

    # The Replicate model that writes each video's scene description (assist.caption), for
    # example google/gemini-2.5-flash. Empty means the site cannot prepare videos and Generate is
    # refused. `fixed` calls no model and sends a neutral description (assist.FIXED_PROMPT) with
    # the `complex-motion` tier. `local*` names are stand-ins for development and tests
    # (assist._local), refused when deployed.
    caption_model: str = Field(default="", pattern=r"^[A-Za-z0-9._:/-]{0,128}$")
    replicate_token: SecretStr | None = None
    replicate_token_parameter: str = ""
    # Its own spending caps in micro-USD per UTC day and month, separate from Gremlin's GPU
    # authority. When a cap is reached, descriptions fall back to the fixed one.
    caption_daily_microusd: int = Field(default=0, ge=0)
    caption_monthly_microusd: int = Field(default=0, ge=0)
    # Replicate's prices for Gemini 2.5 Flash in USD per million tokens (8 November 2026).
    caption_input_usd_per_mtok: float = Field(default=0.30, ge=0)
    caption_output_usd_per_mtok: float = Field(default=2.50, ge=0)

    model_config = SettingsConfigDict(
        env_prefix="GREMLIN_WEB_", env_file=None, extra="forbid", hide_input_in_errors=True
    )

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.role not in {"api", "worker", "local"}:
            raise ValueError("Unknown service role")
        if self.environment not in {"local", "staging", "prod"}:
            raise ValueError("Unknown environment")
        origin = urlparse(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.path not in {"", "/"}
            or origin.query
            or origin.username
        ):
            raise ValueError("The public origin must be a bare HTTP origin")
        self.public_origin = self.public_origin.rstrip("/")
        if self.environment in {"staging", "prod"}:
            if origin.scheme != "https":
                raise ValueError("Deployed environments require an HTTPS origin")
            if not (self.gremlin_base_url and (self.gremlin_token or self.gremlin_token_parameter)):
                raise ValueError("Deployed environments require a Gremlin origin and token")
            if self.caption_model.startswith("local"):
                raise ValueError("The local caption stand-ins are for development and tests only")
            if urlparse(self.gremlin_base_url).hostname in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Deployed environments can't use a loopback Gremlin")
        if "/" in self.caption_model and not (
            self.replicate_token or self.replicate_token_parameter
        ):
            raise ValueError("A Replicate caption model needs a Replicate token")
        return self

    @property
    def secure_cookies(self):
        return self.public_origin.startswith("https://")
