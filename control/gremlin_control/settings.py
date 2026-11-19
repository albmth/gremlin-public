import re
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # `api` serves HTTP; `controller` runs the sweep, doorbells and operator actions; `local`
    # serves HTTP for development and the contract suite and never touches AWS itself.
    role: str = "local"
    environment: str = "local"
    region: str = "eu-north-1"
    table_name: str = "gremlin-local-state"
    media_bucket: str = "gremlin-local-media"
    # Emulator endpoints; empty in AWS.
    dynamodb_endpoint: str = ""
    s3_endpoint: str = ""
    controller_function: str = ""
    monitoring_enabled: bool = False
    moderation_enabled: bool = False
    moderation_region: str = "eu-west-1"
    moderation_guardrail_id: str = ""
    moderation_guardrail_version: str = ""

    public_base_url: str = "http://127.0.0.1:8000"
    # Set by the stack from the deployed commit; reported by capabilities as `build`.
    build_commit: str = ""
    client_token_hashes: dict[str, str] = Field(default_factory=dict)
    # Clients that serve many end users: they must scope every job with X-Gremlin-Owner.
    delegated_clients: list[str] = Field(default_factory=list)
    # `succeed`, `reject` or `fail` runs a GPU-free worker inside the local server, so client
    # developers can reach every outcome without a GPU or provider (gremlin_control.simulator).
    local_worker: str = ""
    imprint_enabled: bool = True
    imprint_daily_jobs: int = 8
    # Serverless dispatch stays off until an operator turns it on with spending authority.
    controller_enabled: bool = False
    # The controller reads the key from SSM; the public API only knows a key is configured.
    imprint_runpod_key: SecretStr | None = None
    imprint_runpod_key_parameter: str = ""
    imprint_endpoint_id: str = ""
    # Operator spending authority; zero keeps paid sessions closed until explicitly granted.
    imprint_hourly_microusd: int = 0
    # Optional cumulative stop-loss for bounded trials; period authority is always required.
    imprint_budget_microusd: int = Field(default=0, ge=0)
    imprint_rolling_budget_microusd: int = Field(default=0, ge=0)
    imprint_monthly_budget_microusd: int = Field(default=0, ge=0)
    imprint_gpu_selection_enabled: bool = True
    imprint_gpu_qualification_enabled: bool = False
    imprint_gpu_qualification_target: str = Field(default="", max_length=120)
    imprint_gpu_queue_timeout_seconds: int = Field(default=660, ge=60, le=900)
    runpod_management_url: str = "https://api.runpod.io/v2"
    runpod_jobs_url: str = "https://api.runpod.ai/v2"
    runpod_graphql_url: str = "https://api.runpod.io/graphql"
    test_hooks: bool = False

    model_config = SettingsConfigDict(
        env_prefix="GREMLIN_", env_file=None, extra="forbid", hide_input_in_errors=True
    )

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.moderation_enabled and (
            self.moderation_region != "eu-west-1"
            or not re.fullmatch(r"[a-z0-9]+", self.moderation_guardrail_id)
            or not re.fullmatch(r"[1-9][0-9]*", self.moderation_guardrail_version)
        ):
            raise ValueError("Moderation requires the Ireland guardrail and a numbered version")
        if self.role not in {"api", "controller", "local"}:
            raise ValueError("Unknown service role")
        if self.environment == "prod" and not self.imprint_gpu_selection_enabled:
            raise ValueError("Production requires automatic qualified GPU selection")
        if self.imprint_gpu_qualification_enabled and self.environment not in {"staging", "test"}:
            raise ValueError("GPU qualification dispatch is restricted to staging")
        if self.imprint_gpu_qualification_target and not (
            self.imprint_gpu_qualification_enabled
            and self.environment in {"staging", "test"}
            and self.imprint_gpu_selection_enabled
        ):
            raise ValueError("A qualification target requires staging GPU qualification selection")
        origin = urlparse(self.public_base_url)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
        ):
            raise ValueError("Public base URL must be an HTTP origin without credentials")
        # The controller hands this origin to workers; the API function never dispatches.
        if self.controller_enabled and self.role != "api" and origin.scheme != "https":
            raise ValueError("GPU dispatch requires an HTTPS public origin")
        self.public_base_url = self.public_base_url.rstrip("/")
        if self.imprint_endpoint_id and not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", self.imprint_endpoint_id
        ):
            raise ValueError("The endpoint ID must be a plain provider identifier")
        for client, digest in self.client_token_hashes.items():
            # Client IDs and owners share the character set, so a delegated scope
            # `client:owner` can never collide with a direct client ID.
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client) or not re.fullmatch(
                r"[0-9a-f]{64}", digest
            ):
                raise ValueError("Client credentials require a client ID and SHA-256 token hash")
        if len(set(self.client_token_hashes.values())) != len(self.client_token_hashes):
            raise ValueError("Each client must have a distinct token")
        if unknown := set(self.delegated_clients) - set(self.client_token_hashes):
            raise ValueError(f"Delegated clients need credentials: {sorted(unknown)}")
        if self.local_worker not in {"", "succeed", "reject", "fail"}:
            raise ValueError("Unknown local worker mode")
        if self.local_worker and self.role != "local":
            raise ValueError("The simulated worker runs only in the local server")
        if self.test_hooks and self.environment != "test":
            raise ValueError("Test hooks are only valid in the test environment")
        return self

    @property
    def provider_configured(self):
        return bool(
            (self.imprint_runpod_key or self.imprint_runpod_key_parameter)
            and self.imprint_endpoint_id
        )
