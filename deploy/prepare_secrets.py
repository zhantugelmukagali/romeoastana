"""First installation only: writes exclusive secret files, never outputs values."""
import base64
import getpass
import os
from pathlib import Path
import secrets

def main():
    directory=Path("deploy/secrets")
    if directory.exists():
        raise SystemExit("Secret directory already exists. Existing installations must not be overwritten.")
    vendor=getpass.getpass("MoySklad Vendor Secret (hidden): ")
    if len(vendor.encode())<32:
        raise SystemExit("Vendor Secret must be at least 32 bytes.")
    passwords={name:secrets.token_urlsafe(36) for name in ("postgres","bonuspro_runtime","bonuspro_installation","bonuspro_migrator")}
    values={"db_password":passwords["postgres"],"vendor_secret":vendor,
            "token_encryption_key":base64.b64encode(secrets.token_bytes(32)).decode()}
    for filename,user in (("admin_database_url","postgres"),("app_database_url","bonuspro_runtime"),
                          ("lifecycle_database_url","bonuspro_installation"),("migration_database_url","bonuspro_migrator")):
        values[filename]="postgresql://"+user+":"+passwords[user]+"@db:5432/bonuspro"
    directory.mkdir(mode=0o700,parents=True)
    for name,value in values.items():
        fd=os.open(directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
        with os.fdopen(fd,"w") as stream:
            stream.write(value+"\n")
        # Application containers run under a dedicated numeric UID.
        os.chown(directory/name,10001,10001)
    print("Secrets created. Keep an encrypted offline backup of the encryption key.")

if __name__=="__main__":
    if os.geteuid()!=0:
        raise SystemExit("Run this first-installation script with sudo on the Linux server.")
    main()
