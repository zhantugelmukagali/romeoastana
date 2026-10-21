"""Probe the local socket using the configured host, not external DNS."""
import os
import urllib.request
from urllib.parse import urlsplit
url=urlsplit(os.environ["PUBLIC_ORIGIN"])
request=urllib.request.Request("http://127.0.0.1:8000/health/ready",headers={"Host":url.netloc})
with urllib.request.urlopen(request,timeout=3) as response:
    if response.status!=200:
        raise SystemExit(1)
