from pydantic_settings import BaseSettings, SettingsConfigDict


class DbSettings(BaseSettings):
    """本地默认对齐 docker-compose；.env 与环境变量可覆盖，凭据不入库。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://audit_rw:localdev@localhost:5432/platform"
    identity_database_url: str = "postgresql+psycopg://identity_rw:localdev@localhost:5432/platform"
    organization_database_url: str = (
        "postgresql+psycopg://organization_rw:localdev@localhost:5432/platform"
    )
    migration_database_url: str = (
        "postgresql+psycopg://platform_owner:localdev@localhost:5432/platform"
    )


class SecuritySettings(BaseSettings):
    """DEV-003 file adapter configuration; secret values never enter settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_material_path: str = "./.localdev-secrets"
