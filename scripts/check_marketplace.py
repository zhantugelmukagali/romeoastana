"""Validate registration materials against the official live descriptor schema."""
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urljoin, urlsplit
from hashlib import sha256
import json
import tempfile
import zipfile
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "marketplace"
SCHEMA = "https://apps-api.moysklad.ru/xml/ns/appstore/app/v2/application-v2.xsd"
parser = etree.XMLParser(resolve_entities=False, no_network=True)
NS = {"xs": "http://www.w3.org/2001/XMLSchema"}
hashes = {}

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory)
    fetched = set()
    def fetch(url):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "apps-api.moysklad.ru"
        assert parsed.path.startswith("/xml/ns/appstore/app/") and not parsed.query
        name = Path(parsed.path).name
        if url in fetched:
            return target / name
        assert len(fetched) < 20
        fetched.add(url)
        with urlopen(Request(url, headers={"User-Agent": "BonusPro-schema-check"}), timeout=30) as response:
            assert response.status == 200
            data = response.read(2_000_001)
        assert len(data) <= 2_000_000
        doc = etree.fromstring(data, parser)
        hashes[url] = sha256(data).hexdigest()
        for node in doc.xpath("//xs:include|//xs:import", namespaces=NS):
            location = node.get("schemaLocation")
            if location:
                child = fetch(urljoin(url, location))
                node.set("schemaLocation", str(child))
        path = target / name
        path.write_bytes(etree.tostring(doc))
        return path
    schema = etree.XMLSchema(etree.parse(str(fetch(SCHEMA)), parser))
    descriptor = etree.parse(str(PACKAGE / "descriptor-draft.xml"), parser)
    schema.assertValid(descriptor)

app = "https://apps-api.moysklad.ru/xml/ns/appstore/app/v2"
q = {"a": app}
assert descriptor.findtext("a:vendorApi/a:endpointBase", namespaces=q) == "https://loyalty.addwise.kz"
assert descriptor.findtext("a:access/a:scope", namespaces=q) == "custom"
frames = descriptor.findall("a:iframes/a:iframe", q)
assert len(frames) == 1 and frames[0].get("sourceUrl") == "https://loyalty.addwise.kz/admin"
assert frames[0].get("useContextKey") == "false"
assert frames[0].find("a:uses/a:user-context", q) is not None
assert descriptor.find("a:widgets", q) is None
source = (ROOT / "backend/bonuspro/api/app.py").read_text()
assert '@app.get("/admin")' in source
assert "/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}" in source

manual = (PACKAGE / "manual/index.html").read_bytes()
with zipfile.ZipFile(PACKAGE / "bonuspro-manual.zip") as archive:
    assert archive.testzip() is None
    assert archive.namelist() == ["index.html"]
    assert archive.read("index.html") == manual
assert (PACKAGE / "bonuspro-manual.zip").stat().st_size < 3_000_000
for required in [b"<h2>", b"moysklad-manual.js", b"moysklad-manual.css"]:
    assert required in manual
icon = PACKAGE / "bonuspro-icon.svg"
assert icon.stat().st_size < 1_000_000
svg = etree.parse(str(icon), parser).getroot()
assert svg.get("width") == svg.get("height") == "512"
assert not svg.xpath("//*[local-name()='script' or local-name()='foreignObject' or local-name()='image']")
report = {"descriptor": "valid against official XSD", "schema_sha256": hashes,
          "manual": "root index.html, CRC and source equality verified",
          "icon": "512x512 SVG under 1 MB",
          "scope": "registration draft only; not live installation acceptance"}
print(json.dumps(report, ensure_ascii=False, indent=2))
