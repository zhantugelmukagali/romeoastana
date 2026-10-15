"""Read-only inspection of public provider transport code; no account credentials."""
import re,urllib.request
BASE="https://cdn-static.moysklad.ru/"
FILES=(
 "app/cdn/r1738-1/7F0B3E689263B3BA34FFF3B61E7779C3.cache.js",
 "react/r1738-1/index.js",
 "react/r1738-1/initial.js",
)
MARKERS=("UserContextRequest","UserContextResponse","context/user","requestUserContext","userContextToken")
def main():
    for path in FILES:
        request=urllib.request.Request(BASE+path,headers={"User-Agent":"BonusPro-public-protocol-audit/1.0"})
        with urllib.request.urlopen(request,timeout=30) as response:
            if response.geturl()!=BASE+path:raise RuntimeError("Unexpected public CDN redirect")
            raw=response.read(48*1024*1024+1)
        if len(raw)>48*1024*1024:raise RuntimeError("Public asset exceeds audit bound")
        value=raw.decode("utf-8",errors="replace")
        print("ASSET",path,"bytes",len(raw),flush=True)
        for marker in MARKERS:
            matches=list(re.finditer(re.escape(marker),value))
            print("MARKER",marker,"count",len(matches),flush=True)
            for match in matches[:12]:
                print("EXCERPT",repr(value[max(0,match.start()-900):match.end()+1300]),flush=True)
if __name__=="__main__":main()
