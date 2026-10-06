"""Experimental encrypted Google Drive backup for ephemeral Docker hosting.

The archive lives in Drive's appDataFolder and requires offline OAuth credentials
plus a separate AES-256 key supplied through environment variables.
"""

import base64
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAGIC = b"LVDRIVE1"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SLOTS = ("a", "b")
CHUNK = 1024 * 1024


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for Drive backup")
    return value


def config():
    instance = required("LUMIVERSE_DRIVE_INSTANCE")
    if not all(c.isalnum() or c in "-_" for c in instance) or len(instance) > 48:
        raise RuntimeError("LUMIVERSE_DRIVE_INSTANCE must use 1–48 letters, digits, _ or -")
    key = base64.b64decode(required("LUMIVERSE_DRIVE_BACKUP_KEY"), validate=True)
    if len(key) != 32:
        raise RuntimeError("LUMIVERSE_DRIVE_BACKUP_KEY must decode to 32 bytes")
    return {
        "instance": instance,
        "key": key,
        "client_id": required("LUMIVERSE_DRIVE_CLIENT_ID"),
        "client_secret": required("LUMIVERSE_DRIVE_CLIENT_SECRET"),
        "refresh_token": required("LUMIVERSE_DRIVE_REFRESH_TOKEN"),
    }


def request_json(url, token, method="GET", payload=None, headers=None):
    body = None if payload is None else json.dumps(payload).encode()
    all_headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    with urlopen(Request(url, body, all_headers, method=method), timeout=60) as response:
        raw = response.read()
        return (json.loads(raw) if raw else {}), dict(response.headers)


def access_token(cfg):
    body = urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    with urlopen(Request("https://oauth2.googleapis.com/token", body), timeout=30) as response:
        return json.load(response)["access_token"]


def slot_name(cfg, slot):
    return f"lumiverse-{cfg['instance']}-{slot}.tar.gz.aes"


def list_backups(cfg, token):
    names = [slot_name(cfg, slot) for slot in SLOTS]
    quoted = [name.replace("'", "\\'") for name in names]
    query = f"(name = '{quoted[0]}' or name = '{quoted[1]}') and trashed = false"
    params = urlencode({
        "spaces": "appDataFolder",
        "q": query,
        "fields": "nextPageToken,files(id,name,modifiedTime,md5Checksum,size)",
        "pageSize": "100",
    })
    result, _ = request_json(f"{DRIVE_API}?{params}", token)
    if result.get("nextPageToken"):
        raise RuntimeError("Unexpectedly many backup files in Drive")
    files = [item for item in result.get("files", []) if item.get("name") in names]
    return sorted(files, key=lambda item: item.get("modifiedTime", ""), reverse=True)


def archive_data(data_dir, archive_path):
    if not (data_dir / "lumiverse.db").is_file():
        raise RuntimeError("Lumiverse database does not exist yet")
    if not (data_dir / "lumiverse.identity").is_file():
        raise RuntimeError("Lumiverse encryption identity does not exist yet")
    with tempfile.TemporaryDirectory(prefix="lumiverse-sqlite-") as scratch:
        snapshots = {}
        for path in data_dir.rglob("*"):
            if path.is_symlink():
                raise RuntimeError(f"Refusing to back up symlink: {path}")
            if path.is_file() and path.suffix.lower() in (".db", ".sqlite"):
                dest = Path(scratch) / str(len(snapshots))
                with closing(sqlite3.connect(str(path))) as source:
                    with closing(sqlite3.connect(dest)) as target:
                        source.backup(target)
                snapshots[path.relative_to(data_dir)] = dest

        with tarfile.open(archive_path, "w:gz") as archive:
            for path in data_dir.rglob("*"):
                relative = path.relative_to(data_dir)
                if path.is_dir():
                    continue
                if path in [data_dir / name for name in ("lumiverse.db-wal", "lumiverse.db-shm")]:
                    continue
                if path.name.endswith(("-wal", "-shm", "-journal")):
                    continue
                if ".bun-transpiler-cache" in relative.parts:
                    continue
                if path.is_file() and relative not in snapshots:
                    archive.add(path, arcname=relative.as_posix(), recursive=False)
            for relative, snapshot in snapshots.items():
                archive.add(snapshot, arcname=relative.as_posix(), recursive=False)


def encrypt_file(source, dest, key):
    nonce = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with open(source, "rb") as reader, open(dest, "wb") as writer:
        writer.write(MAGIC + nonce)
        while chunk := reader.read(CHUNK):
            writer.write(cipher.update(chunk))
        writer.write(cipher.finalize())
        writer.write(cipher.tag)


def decrypt_file(source, dest, key):
    size = source.stat().st_size
    if size < len(MAGIC) + 12 + 16:
        raise RuntimeError("Backup is truncated")
    with open(source, "rb") as reader:
        if reader.read(len(MAGIC)) != MAGIC:
            raise RuntimeError("Invalid backup format")
        nonce = reader.read(12)
        reader.seek(size - 16)
        tag = reader.read(16)
        reader.seek(len(MAGIC) + 12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        remaining = size - len(MAGIC) - 12 - 16
        with open(dest, "wb") as writer:
            while remaining:
                chunk = reader.read(min(CHUNK, remaining))
                if not chunk:
                    raise RuntimeError("Backup is truncated")
                writer.write(decryptor.update(chunk))
                remaining -= len(chunk)
            writer.write(decryptor.finalize())


def md5_file(path):
    digest = hashlib.md5()  # Drive returns MD5 for binary files.
    with open(path, "rb") as reader:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def upload_backup(cfg, token, path, prior):
    occupied = {item["name"]: item for item in prior}
    chosen = min(SLOTS, key=lambda slot: occupied.get(slot_name(cfg, slot), {}).get("modifiedTime", ""))
    name = slot_name(cfg, chosen)
    existing = occupied.get(name)
    endpoint = DRIVE_UPLOAD + ("/" + quote(existing["id"], safe="") if existing else "")
    url = endpoint + "?uploadType=resumable&fields=id,name,md5Checksum,modifiedTime"
    metadata = {} if existing else {"name": name, "parents": ["appDataFolder"]}
    _, headers = request_json(url, token, "PATCH" if existing else "POST", metadata, {
        "X-Upload-Content-Type": "application/octet-stream",
        "X-Upload-Content-Length": str(path.stat().st_size),
    })
    location = headers.get("Location") or headers.get("location")
    parsed = urlparse(location or "")
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".googleapis.com"):
        raise RuntimeError("Drive returned an invalid upload URL")
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=900)
    try:
        with open(path, "rb") as reader:
            connection.request("PUT", parsed.path + ("?" + parsed.query if parsed.query else ""),
                               body=reader, headers={
                                   "Authorization": f"Bearer {token}",
                                   "Content-Type": "application/octet-stream",
                                   "Content-Length": str(path.stat().st_size),
                               })
            response = connection.getresponse()
            result = json.load(response)
            if response.status not in (200, 201):
                raise RuntimeError(f"Drive upload failed: HTTP {response.status}")
    finally:
        connection.close()
    if result.get("md5Checksum") != md5_file(path):
        raise RuntimeError("Drive upload checksum mismatch")
    return result


def download_backup(item, token, destination):
    url = f"{DRIVE_API}/{quote(item['id'], safe='')}?alt=media"
    with urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=900) as source:
        with open(destination, "wb") as target:
            shutil.copyfileobj(source, target, CHUNK)
    if item.get("md5Checksum") != md5_file(destination):
        raise RuntimeError("Downloaded backup checksum mismatch")


def extract_safe(archive_path, destination):
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            relative = Path(member.name)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise RuntimeError("Unsafe backup archive path")
            if not (member.isdir() or member.isfile()):
                raise RuntimeError("Backup archive contains a link or special file")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output, CHUNK)


def restore_if_empty(cfg, data_dir):
    if any(data_dir.iterdir()):
        print("[drive-backup] Local data exists; skipping cloud restore", flush=True)
        return
    token = access_token(cfg)
    backups = list_backups(cfg, token)
    if not backups:
        print("[drive-backup] No backup found; starting a new instance", flush=True)
        return
    for item in backups:
        try:
            with tempfile.TemporaryDirectory(prefix="lumiverse-restore-") as scratch:
                scratch = Path(scratch)
                encrypted = scratch / "backup.aes"
                archive = scratch / "backup.tar.gz"
                extracted = scratch / "data"
                extracted.mkdir()
                download_backup(item, token, encrypted)
                decrypt_file(encrypted, archive, cfg["key"])
                extract_safe(archive, extracted)
                if not (extracted / "lumiverse.db").is_file() or not (extracted / "lumiverse.identity").is_file():
                    raise RuntimeError("Backup lacks database or encryption identity")
                for path in extracted.iterdir():
                    shutil.move(str(path), data_dir / path.name)
            print(f"[drive-backup] Restored backup {item['name']}", flush=True)
            return
        except Exception as error:
            print(f"[drive-backup] Restore candidate failed: {error}", flush=True)
    raise RuntimeError("No valid Drive backup could be restored; refusing to start empty")


def backup_once(cfg, data_dir):
    token = access_token(cfg)
    prior = list_backups(cfg, token)
    with tempfile.TemporaryDirectory(prefix="lumiverse-backup-") as scratch:
        scratch = Path(scratch)
        archive = scratch / "backup.tar.gz"
        encrypted = scratch / "backup.aes"
        archive_data(data_dir, archive)
        encrypt_file(archive, encrypted, cfg["key"])
        result = upload_backup(cfg, token, encrypted, prior)
    print(f"[drive-backup] Uploaded and verified {result['name']}", flush=True)


def main():
    cfg = config()
    data_dir = Path(os.environ.get("DATA_DIR", "/app/data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    restore_if_empty(cfg, data_dir)
    child = subprocess.Popen(["bun", "run", "src/index.ts"])

    def stop(signum, _frame):
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    next_backup = time.monotonic() + 60
    interval = max(60, int(os.environ.get("LUMIVERSE_DRIVE_BACKUP_SECONDS", "300")))
    while child.poll() is None:
        if time.monotonic() >= next_backup:
            try:
                backup_once(cfg, data_dir)
            except Exception as error:
                print(f"[drive-backup] Backup failed: {error}", flush=True)
            next_backup = time.monotonic() + interval
        time.sleep(2)
    raise SystemExit(child.returncode)


if __name__ == "__main__":
    main()
