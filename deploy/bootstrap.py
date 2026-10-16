"""Provision restricted database roles; never run with customer database credentials."""
from psycopg import connect, sql
from psycopg.conninfo import conninfo_to_dict
from bonuspro.settings import secret

def main():
    owner=secret("DATABASE_ADMIN_URL")
    base=conninfo_to_dict(owner)
    configs={}
    for name,env in (("bonuspro_runtime","DATABASE_URL"),("bonuspro_installation","LIFECYCLE_DATABASE_URL"),
                     ("bonuspro_migrator","DATABASE_MIGRATION_URL")):
        data=conninfo_to_dict(secret(env))
        if data.get("user")!=name or not data.get("password") or any(
            data.get(key)!=base.get(key) for key in ("host","port","dbname")
        ):
            raise ValueError("database role configuration mismatch")
        configs[name]=data
    with connect(owner,autocommit=True) as conn:
        for group in ("bonuspro_app","bonuspro_lifecycle"):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(group,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(group)))
        for name,data in configs.items():
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(name,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(name)))
            conn.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                                  "NOBYPASSRLS CONNECTION LIMIT 24 PASSWORD {}").format(
                                      sql.Identifier(name),sql.Literal(data["password"])))
        conn.execute("GRANT bonuspro_app TO bonuspro_runtime")
        conn.execute("GRANT bonuspro_lifecycle TO bonuspro_installation")
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO bonuspro_runtime,bonuspro_installation,bonuspro_migrator")
                     .format(sql.Identifier(base["dbname"])))
        conn.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO bonuspro_migrator").format(sql.Identifier(base["dbname"])))
        conn.execute("GRANT USAGE,CREATE ON SCHEMA public TO bonuspro_migrator")
    print("Database roles provisioned; credentials not printed.")

if __name__=="__main__":
    main()
