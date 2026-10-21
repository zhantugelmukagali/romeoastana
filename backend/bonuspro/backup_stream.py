"""Bounded authenticated backup stream. Every file derives a separate AES-GCM key.

Keep the master key outside the VPS. Decrypt completely to a private temporary
file and check exit status before restoring: a streaming reader can detect tail
truncation only at the authenticated end record.
"""
import argparse,base64,os,struct,sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
MAGIC=b"BPBK0001"
CHUNK=1024*1024
HEADER=48

class InvalidBackup(ValueError):pass

def read_exact(source,size):
    result=bytearray()
    while len(result)<size:
        value=source.read(size-len(result))
        if not value:raise InvalidBackup("truncated backup")
        result.extend(value)
    return bytes(result)

def cipher(key,header):
    if len(key)!=32:raise InvalidBackup("invalid master key")
    derived=HKDF(algorithm=hashes.SHA256(),length=32,salt=header[8:40],
                 info=b"bonuspro/backup-stream/v1").derive(key)
    return AESGCM(derived)

def encrypt(source,target,key):
    header=MAGIC+os.urandom(32)+os.urandom(8)
    box=cipher(key,header);target.write(header);index=0
    while True:
        value=source.read(CHUNK)
        if not isinstance(value,bytes):raise InvalidBackup("binary stream required")
        if index>=2**32:raise InvalidBackup("backup too large")
        end=not value
        counter=struct.pack(">I",index)
        encrypted=box.encrypt(header[40:48]+counter,value,header+counter+(b"end" if end else b"data"))
        target.write(struct.pack(">I",len(encrypted)));target.write(encrypted)
        if end:return
        index+=1

def decrypt(source,target,key):
    header=read_exact(source,HEADER)
    if not header.startswith(MAGIC):raise InvalidBackup("unsupported backup")
    box=cipher(key,header);index=0
    while True:
        if index>=2**32:raise InvalidBackup("backup too large")
        length=struct.unpack(">I",read_exact(source,4))[0]
        if not 16<=length<=CHUNK+16:raise InvalidBackup("invalid record length")
        encrypted=read_exact(source,length);counter=struct.pack(">I",index);end=length==16
        try:value=box.decrypt(header[40:48]+counter,encrypted,header+counter+(b"end" if end else b"data"))
        except Exception:raise InvalidBackup("authentication failed") from None
        if end:
            if source.read(1):raise InvalidBackup("trailing backup bytes")
            return
        target.write(value);index+=1

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("operation",choices=("encrypt","decrypt"))
    parser.add_argument("--key-file",required=True)
    args=parser.parse_args()
    try:
        key=base64.b64decode(Path(args.key_file).read_text().strip(),validate=True)
        (encrypt if args.operation=="encrypt" else decrypt)(sys.stdin.buffer,sys.stdout.buffer,key)
        sys.stdout.buffer.flush()
    except Exception:
        # Never print exception text, key contents or partial plaintext to stderr.
        print("backup_stream_failed",file=sys.stderr)
        raise SystemExit(1)
if __name__=="__main__":main()
