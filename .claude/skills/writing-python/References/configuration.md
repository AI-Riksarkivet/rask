# Configuration

Externalize configuration from code with environment variables, parsed into typed `pydantic-settings` objects at startup. Fail fast on missing required values.

## Contents

- Typed settings with Pydantic
- Fail fast at startup
- Local development defaults
- Namespaced env vars
- Type coercion
- Environment-specific behavior
- Nested configuration groups
- Secrets from files (containers)
- Custom cross-field validation
- Summary
- Gotchas

## Typed settings with Pydantic

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, ValidationError
import sys

class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    # Database
    db_host: str = Field(alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_name: str = Field(alias="DB_NAME")
    db_user: str = Field(alias="DB_USER")
    db_password: str = Field(alias="DB_PASSWORD")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379", alias="REDIS_URL")

    # API keys
    api_secret_key: str = Field(alias="API_SECRET_KEY")

    # Feature flags
    enable_new_feature: bool = Field(default=False, alias="ENABLE_NEW_FEATURE")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


try:
    settings = Settings()
except ValidationError as e:
    print(f"Configuration error:\n{e}", file=sys.stderr)
    sys.exit(1)
```

Import the singleton everywhere; don't sprinkle `os.getenv` through the codebase.

```python
from myapp.config import settings

def get_database_connection():
    return connect(host=settings.db_host, port=settings.db_port, database=settings.db_name)
```

## Fail fast at startup

```python
try:
    settings = Settings()
except ValidationError as e:
    print("=" * 60, file=sys.stderr)
    print("CONFIGURATION ERROR", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    for error in e.errors():
        field = error["loc"][0]
        print(f"  - {field}: {error['msg']}", file=sys.stderr)
    print("\nPlease set the required environment variables.", file=sys.stderr)
    sys.exit(1)
```

A clear error at startup beats a cryptic `None` mid-request.

## Local development defaults

Sensible defaults for local dev; explicit values required for secrets.

```python
class Settings(BaseSettings):
    db_host: str = Field(default="localhost", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")

    # No default — must be set
    db_password: str = Field(alias="DB_PASSWORD")
    api_secret_key: str = Field(alias="API_SECRET_KEY")

    debug: bool = Field(default=False, alias="DEBUG")

    model_config = SettingsConfigDict(env_file=".env")
```

`.env` for local (gitignored):

```bash
DB_PASSWORD=local_dev_password
API_SECRET_KEY=dev-secret-key
DEBUG=true
```

## Namespaced env vars

```bash
DB_HOST=localhost
DB_PORT=5432
DB_NAME=myapp

REDIS_URL=redis://localhost:6379
REDIS_MAX_CONNECTIONS=10

AUTH_SECRET_KEY=...
AUTH_TOKEN_EXPIRY_SECONDS=3600

FEATURE_NEW_CHECKOUT=true
```

`env | grep DB_` becomes a useful debugging aid.

## Type coercion

Pydantic handles common conversions automatically.

```python
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator

class Settings(BaseSettings):
    debug: bool = False  # "true"/"1"/"yes" -> True
    max_connections: int = 100  # string -> int

    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [host.strip() for host in v.split(",") if host.strip()]
        return v
```

```bash
ALLOWED_HOSTS=example.com,api.example.com,localhost
MAX_CONNECTIONS=50
DEBUG=true
```

## Environment-specific behavior

```python
from enum import Enum
from pydantic_settings import BaseSettings
from pydantic import Field, computed_field

class Environment(str, Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"

class Settings(BaseSettings):
    environment: Environment = Field(default=Environment.LOCAL, alias="ENVIRONMENT")
    log_level: str = Field(default="DEBUG", alias="LOG_LEVEL")

    @computed_field
    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @computed_field
    @property
    def is_local(self) -> bool:
        return self.environment == Environment.LOCAL


if settings.is_production:
    configure_production_logging()
else:
    configure_debug_logging()
```

## Nested configuration groups

```python
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

class DatabaseSettings(BaseModel):
    host: str = "localhost"
    port: int = 5432
    name: str
    user: str
    password: str

class RedisSettings(BaseModel):
    url: str = "redis://localhost:6379"
    max_connections: int = 10

class Settings(BaseSettings):
    database: DatabaseSettings
    redis: RedisSettings
    debug: bool = False

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_file=".env",
    )
```

```bash
DATABASE__HOST=db.example.com
DATABASE__PORT=5432
DATABASE__NAME=myapp
DATABASE__USER=admin
DATABASE__PASSWORD=secret
REDIS__URL=redis://redis.example.com:6379
```

## Secrets from files (containers)

For Docker/K8s secret mounts:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    db_password: str = Field(alias="DB_PASSWORD")

    model_config = SettingsConfigDict(
        secrets_dir="/run/secrets",
    )
```

Pydantic checks `/run/secrets/db_password` when the env var isn't set.

## Custom cross-field validation

```python
from pydantic_settings import BaseSettings
from pydantic import Field, model_validator

class Settings(BaseSettings):
    db_host: str = Field(alias="DB_HOST")
    db_port: int = Field(alias="DB_PORT")
    read_replica_host: str | None = Field(default=None, alias="READ_REPLICA_HOST")
    read_replica_port: int = Field(default=5432, alias="READ_REPLICA_PORT")

    @model_validator(mode="after")
    def validate_replica_settings(self):
        if (
            self.read_replica_host
            and self.read_replica_port == self.db_port
            and self.read_replica_host == self.db_host
        ):
            raise ValueError("Read replica cannot be the same as primary database")
        return self
```

## Summary

1. **Never hardcode config** — all environment-specific values from env vars.
2. **Use `pydantic-settings`** with validation.
3. **Fail fast** on missing required config at startup.
4. **Sensible local defaults**; explicit values required for secrets.
5. **Never commit secrets** — `.env` (gitignored) or secret managers.
6. **Namespace env vars** for clarity (`DB_HOST`, `REDIS_URL`).
7. **Import a singleton** — don't `os.getenv` throughout the codebase.
8. **Document required env vars** in the README.
9. **Use `secrets_dir`** for container secret mounts.

## Gotchas

- **`pydantic-settings` reads env at instantiation, not import** — but tests that monkeypatch env _after_ instantiating see cached values. Instantiate after patching, or build a new `Settings()` in the test.
- **Nested `BaseSettings` need `env_nested_delimiter` explicitly set** — without it, `DB__HOST` won't auto-bind to `Settings.db.host`.
- **`SecretStr` redacts in `__repr__` but NOT in `__str__`** — `print(secret)` leaks; `f"{secret}"` leaks; only `repr(secret)` and `secret.get_secret_value()` are explicit.
- **`.env` file precedence vs process env**: pydantic-settings reads `.env` first then overlays process env. A CI secret wins over a developer's `.env` — but stale `.env` values stick if process env is missing the key.
