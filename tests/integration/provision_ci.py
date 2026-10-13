"""Provision ONLY the disposable CI database. Never use with customer data."""
import os
import psycopg

with psycopg.connect(os.environ["TEST_ADMIN_DSN"], autocommit=True) as conn:
    if conn.info.dbname != "bonuspro_ci":
        raise RuntimeError("CI bootstrap requires database bonuspro_ci")
    conn.execute("CREATE ROLE bonuspro_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS")
    # Synthetic credential restricted to the disposable GitHub Actions service.
    conn.execute("CREATE ROLE bonuspro_ci_app LOGIN PASSWORD 'ci-only-ephemeral' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS")
    conn.execute("GRANT bonuspro_app TO bonuspro_ci_app")
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    conn.execute("CREATE ROLE bonuspro_lifecycle NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS")
    conn.execute("CREATE ROLE bonuspro_ci_lifecycle LOGIN PASSWORD 'ci-only-lifecycle' NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS")
    conn.execute("GRANT bonuspro_lifecycle TO bonuspro_ci_lifecycle")
