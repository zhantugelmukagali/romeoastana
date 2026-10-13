"""Switch this known VPS from pilot Caddy to release Caddy; rollback on failure.
Run as root. Never modifies application/database containers or secret files.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OLD_WEB = "romeo-customer-web-1"
DOMAIN = "loyalty.addwise.kz"
COMPOSE = ["docker", "compose", "--env-file", ".env.release", "-p", "bonuspro-release",
           "-f", "compose.release.yaml", "-f", "compose.https.yaml"]


def run(args):
    subprocess.run(args, check=True, timeout=180)


def capture(args):
    return subprocess.check_output(args, text=True, timeout=15)


def readiness(url):
    request = urllib.request.Request(url, headers={"Host": DOMAIN})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200 or json.load(response) != {"status": "ready"}:
            raise RuntimeError("Unexpected readiness response")


def https_probe():
    # System TLS trust and hostname verification remain enabled.
    for attempt in range(12):
        try:
            readiness("https://" + DOMAIN + "/health/ready")
            return
        except Exception:
            if attempt == 11:
                raise
            time.sleep(3)


def switch(runner, probe):
    try:
        runner(["docker", "stop", "--time", "20", OLD_WEB])
        runner([*COMPOSE, "up", "-d", "--no-deps", "web"])
        probe()
    except BaseException:
        # Try restoring the old listener even if stopping the new listener fails.
        stop_error = None
        try:
            runner([*COMPOSE, "stop", "web"])
        except Exception as error:
            stop_error = error
        runner(["docker", "start", OLD_WEB])
        if stop_error:
            raise RuntimeError("Rollback attempted; inspect both web containers") from stop_error
        print("Switch failed. Previous web container restarted; verify the previous site.")
        raise


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo python3 deploy/switch_https.py")
    os.chdir(ROOT)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    old = json.loads(capture(["docker", "inspect", OLD_WEB]))[0]
    if not old["State"]["Running"]:
        raise SystemExit("Previous web container is not running. Inspect the current state before switching.")
    mounts = {m["Destination"]: m for m in old["Mounts"]}
    for destination, name in (("/data", "romeo-customer_caddydata"),
                              ("/config", "romeo-customer_caddyconfig")):
        if mounts.get(destination, {}).get("Name") != name:
            raise SystemExit("Unexpected certificate volume. No changes made.")
    existing = capture(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=bonuspro-release",
                        "--filter", "label=com.docker.compose.service=web"]).strip()
    if existing:
        raise SystemExit("Release web container already exists. Inspect it before repeating the switch.")
    readiness("http://127.0.0.1:18080/health/ready")
    run([*COMPOSE, "config", "--quiet"])
    run([*COMPOSE, "pull", "web"])
    # Validate in a disposable container, without binding ports or mounting live TLS storage.
    run(["docker", "run", "--rm", "-v",
         str(ROOT / "deploy/Caddyfile.release") + ":/etc/caddy/Caddyfile:ro",
         "caddy:2-alpine", "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
         "--adapter", "caddyfile"])
    archive = ROOT / "backups" / ("https-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    archive.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = Path(mounts["/etc/caddy/Caddyfile"]["Source"])
    target = archive / "Caddyfile.pilot"
    target.write_bytes(source.read_bytes())
    target.chmod(0o600)
    # Existing TLS volumes and the previous container remain available for rollback.
    switch(run, https_probe)
    run([*COMPOSE, "ps"])
    print('HTTPS_READY: https://loyalty.addwise.kz/health/ready')
    print("Server connectivity only. This does not verify a MoySklad installation or a sale.")


if __name__ == "__main__":
    main()
