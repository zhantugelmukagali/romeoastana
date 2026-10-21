import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from bonuspro.security import Secrets


def secret(name: str) -> str:
    # Container secrets can be mounted as files; never print loaded values.
    filename = os.environ.get(name+"_FILE")
    if filename and os.environ.get(name):
        raise ValueError(f"configure only one source for {name}")
    value = Path(filename).read_text().strip() if filename else os.environ.get(name,"")
    if not value:
        raise ValueError(f"missing {name}")
    return value


@dataclass(frozen=True)
class Settings:
    origin: str
    credentials: Secrets = field(repr=False)
    database_url: str = field(repr=False)
    lifecycle_database_url: str = field(repr=False)

    def __post_init__(self):
        url = urlsplit(self.origin)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("","/") or url.query or url.fragment:
            raise ValueError("PUBLIC_ORIGIN must be a plain HTTPS origin")
        for dsn in (self.database_url,self.lifecycle_database_url):
            if not dsn.startswith(("postgresql://","postgres://")):
                raise ValueError("PostgreSQL connection URL is required")

    @classmethod
    def from_env(cls):
        return cls(
            origin=os.environ["PUBLIC_ORIGIN"].rstrip("/"),
            credentials=Secrets(os.environ["MOYSKLAD_APP_ID"],os.environ["MOYSKLAD_APP_UID"],
                                secret("MOYSKLAD_VENDOR_SECRET"),
                                base64.b64decode(secret("TOKEN_ENCRYPTION_KEY"),validate=True)),
            database_url=secret("DATABASE_URL"),
            lifecycle_database_url=secret("LIFECYCLE_DATABASE_URL"),
        )
