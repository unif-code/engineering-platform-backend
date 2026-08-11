from alembic import context
from sqlalchemy import create_engine

from control_plane.app.shared.db.settings import DbSettings


def run_migrations_online() -> None:
    engine = create_engine(DbSettings().migration_database_url)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
