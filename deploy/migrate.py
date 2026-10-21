"""Run migrations explicitly with a dedicated owner credential."""
import os
from alembic import command
from alembic.config import Config
from bonuspro.settings import secret

dsn=secret("DATABASE_MIGRATION_URL")
if dsn.startswith("postgresql://"):
    dsn=dsn.replace("postgresql://","postgresql+psycopg://",1)
os.environ["DATABASE_MIGRATION_URL"]=dsn
command.upgrade(Config("alembic.ini"),"head")
