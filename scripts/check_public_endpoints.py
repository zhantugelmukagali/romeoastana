"""Read-only public reachability diagnostics; never authenticates to the VPS."""
import http.client
import json
import socket
import ssl

DOMAIN = "loyalty.addwise.kz"
SERVER = "212.116.234.211"
report = {"domain": DOMAIN, "expected_ipv4": SERVER}
try:
    addresses = sorted({row[4][0] for row in socket.getaddrinfo(DOMAIN, 443, socket.AF_INET)})
    report["dns"] = {"ipv4": addresses, "expected_address_present": SERVER in addresses}
except OSError as exc:
    report["dns"] = {"error": str(exc)}
try:
    context = ssl.create_default_context()
    with socket.create_connection((DOMAIN, 443), timeout=10) as tcp:
        with context.wrap_socket(tcp, server_hostname=DOMAIN) as tls:
            cert = tls.getpeercert()
            report["tls"] = {"verified": True, "expires": cert["notAfter"], "protocol": tls.version()}
    connection = http.client.HTTPSConnection(DOMAIN, timeout=10, context=context)
    connection.request("HEAD", "/")
    response = connection.getresponse()
    report["https"] = {"status": response.status}
    connection.close()
except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
    report["https"] = {"error": str(exc)}
try:
    with socket.create_connection((SERVER, 22), timeout=10) as tcp:
        tcp.settimeout(10)
        # A banner is reachability evidence, not host-key verification or login.
        banner = tcp.recv(255).decode("ascii", errors="replace").splitlines()[0]
        report["ssh"] = {"reachable": banner.startswith("SSH-"), "banner": banner,
                         "authenticated": False, "host_key_verified": False}
except (OSError, IndexError) as exc:
    report["ssh"] = {"error": str(exc), "authenticated": False}
print(json.dumps(report, ensure_ascii=False, indent=2))
