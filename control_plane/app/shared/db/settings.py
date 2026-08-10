from pydantic_settings import BaseSettings, SettingsConfigDict


class DbSettings(BaseSettings):
    """本地默认对齐 docker-compose；.env 与环境变量可覆盖，凭据不入库。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://audit_rw:localdev@localhost:5432/platform"
    migration_database_url: str = (
        "postgresql+psycopg://platform_owner:localdev@localhost:5432/platform"
    )
