import os
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

# Separate migration credential; never fall back to the application connection.
url = os.environ["DATABASE_MIGRATION_URL"]
with create_engine(url, poolclass=NullPool).connect() as connection:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
