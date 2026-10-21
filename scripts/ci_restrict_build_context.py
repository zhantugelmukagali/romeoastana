"""Reproduce a private (umask 077) build context on disposable CI runners."""
import os
from pathlib import Path

def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Disposable GitHub runner required")
    for directory in ("backend", "web", "migrations", "deploy"):
        root = Path(directory)
        for path in [root, *root.rglob("*")]:
            if path.is_symlink():
                raise RuntimeError("Unexpected build-context symlink")
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o700 if path.is_dir() or executable else 0o600)
    for filename in ("requirements-server.lock", "alembic.ini"):
        Path(filename).chmod(0o600)
    assert Path("backend/bonuspro/api/app.py").stat().st_mode & 0o777 == 0o600
    assert Path("web/assets/bridge.js").stat().st_mode & 0o777 == 0o600
    print("Restricted build context prepared: app.py and bridge.js are 0600")

if __name__ == "__main__":
    main()
