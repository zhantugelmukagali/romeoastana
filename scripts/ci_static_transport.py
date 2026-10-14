"""Read full static bodies through Uvicorn and real Caddy TLS in disposable CI."""
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request


def check_static_transport():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Disposable GitHub runner required")
    files = [("/admin", Path("web/index.html"))]
    files += [("/assets/" + p.name, p) for p in sorted(Path("web/assets").iterdir()) if p.is_file()]
    for url, path in files:
        request = urllib.request.Request("http://127.0.0.1:18080" + url,
                                         headers={"Host": "ci.bonuspro.test"})
        with urllib.request.urlopen(request, timeout=10) as response:
            actual = response.read()
            assert response.status == 200, url
            assert actual == path.read_bytes(), ("Incomplete upstream body", url, len(actual))
            assert int(response.headers["Content-Length"]) == len(actual), url
    print("All upstream static assets received byte-for-byte", flush=True)

    container = "bonuspro-ci-static-proxy"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "Caddyfile"
        config.write_text("""https://ci.bonuspro.test {
    tls internal
    encode zstd gzip
    reverse_proxy app:8000
}
""")
        started = False
        try:
            subprocess.run(["docker", "run", "-d", "--name", container,
                            "--network", "bonuspro-ci-container_default",
                            "-p", "127.0.0.1:18443:443",
                            "-v", str(config.resolve()) + ":/etc/caddy/Caddyfile:ro",
                            "caddy:2-alpine"], check=True, timeout=120)
            started = True
            common = ["curl", "--noproxy", "*", "--insecure", "--silent", "--show-error",
                      "--fail", "--connect-timeout", "3", "--max-time", "15",
                      "--resolve", "ci.bonuspro.test:18443:127.0.0.1"]
            # Insecure is limited to a disposable local CA; production TLS is unchanged.
            subprocess.run([*common, "--retry", "10", "--retry-connrefused", "--retry-delay", "1",
                            "--output", "/dev/null", "https://ci.bonuspro.test:18443/health/ready"],
                           check=True, timeout=180)
            for protocol, expected in (("--http1.1", "1.1"), ("--http2", "2")):
                for compressed in (False, True):
                    for url, path in files:
                        output = root / "body"
                        result = subprocess.run(
                            [*common, protocol, *(["--compressed"] if compressed else []),
                             "--output", str(output), "--write-out", "%{http_version}",
                             "https://ci.bonuspro.test:18443" + url],
                            check=True, capture_output=True, text=True, timeout=20)
                        assert result.stdout == expected, ("Unexpected HTTP version", result.stdout)
                        assert output.read_bytes() == path.read_bytes(), ("Incomplete proxy body", url, protocol)
                    print("All TLS static assets byte-for-byte:", protocol,
                          "compressed=" + str(compressed), flush=True)
        finally:
            if started:
                subprocess.run(["docker", "logs", "--tail", "40", container], check=False, timeout=20)
                subprocess.run(["docker", "rm", "-f", "-v", container], check=True, timeout=30)
