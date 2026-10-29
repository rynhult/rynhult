from contextlib import nullcontext

from alembic import context
from sqlalchemy import engine_from_config, pool

from acc.server.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection") or engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with (nullcontext(connectable) if config.attributes.get("connection") is not None
          else connectable.connect()) as connection:
        # pysqlite's legacy transaction mode does not BEGIN for DDL. Without an explicit
        # transaction a failed batch migration leaves tables behind even though data rolls back.
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                                  transactional_ddl=True)
                context.run_migrations()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        else:
            context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()


run_migrations_online()
