"""Build the uploadable manual from the current HTML source."""
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
source = ROOT / "manual" / "index.html"
target = ROOT / "bonuspro-manual.zip"

def build():
    content = source.read_bytes()
    if len(content) > 2_000_000:
        raise ValueError("manual source exceeds the upload budget")
    entry = ZipInfo("index.html", date_time=(2026, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    entry.compress_type = ZIP_DEFLATED
    with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(entry, content)
    print("Built marketplace/bonuspro-manual.zip from manual/index.html")

if __name__ == "__main__":
    build()
