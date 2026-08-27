"""Configuration. No secrets live in the database (spec §11)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDIUM_", env_file=".env", extra="ignore")

    #: Application connection. Reads and writes everything except the
    #: append-only tables, where it may only INSERT (migration 0003).
    database_url: str = "postgresql+psycopg://studium:studium@localhost:5432/studium"

    #: Owner connection used by the retention, erasure and cost-roll-up jobs,
    #: which must DELETE from append-only tables. Defaults to the app URL for
    #: local development, where both roles are the same superuser.
    owner_database_url: str | None = None

    #: Postgres 16.x. Not 15, not 17-beta (§4).
    minimum_server_version: tuple[int, int] = (16, 0)

    echo_sql: bool = False

    #: Where ingestion keeps source PDFs and their intermediate artefacts
    #: (ingestion §4 "File storage"). A Fly volume at MVP; the data layer
    #: treats ``sources.storage_path`` as opaque (§6.3), so moving this to R2
    #: later changes what the path means to this process and nothing else.
    #:
    #: Overridable per-environment because the tests need it pointed at a
    #: temporary directory -- a default of "/data" would have Tier 2 writing
    #: into the deployment's volume layout on a developer's machine.
    source_storage_root: str = "/data/sources"

    @property
    def jobs_database_url(self) -> str:
        return self.owner_database_url or self.database_url


settings = Settings()
