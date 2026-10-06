import importlib.util
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tarfile
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "src" / "drive_backup.py"
spec = importlib.util.spec_from_file_location("drive_backup", MODULE)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class DriveBackupArchiveTests(unittest.TestCase):
    def test_live_sqlite_and_files_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            data = root / "data"
            data.mkdir()
            (data / "lumiverse.identity").write_bytes(b"test identity")
            (data / "owner.credentials").write_bytes(b"test credentials")
            (data / "images").mkdir()
            (data / "images" / "avatar.png").write_bytes(b"image bytes")
            connection = sqlite3.connect(data / "lumiverse.db")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE chats (message TEXT)")
            connection.execute("INSERT INTO chats VALUES ('after setup')")
            connection.commit()
            archive = root / "backup.tar.gz"
            encrypted = root / "backup.aes"
            decrypted = root / "decrypted.tar.gz"
            restored = root / "restored"
            restored.mkdir()
            key = os.urandom(32)

            try:
                backup.archive_data(data, archive)
                backup.encrypt_file(archive, encrypted, key)
                backup.decrypt_file(encrypted, decrypted, key)
                backup.extract_safe(decrypted, restored)
            finally:
                connection.close()

            self.assertEqual((restored / "lumiverse.identity").read_bytes(), b"test identity")
            self.assertEqual((restored / "owner.credentials").read_bytes(), b"test credentials")
            self.assertEqual((restored / "images" / "avatar.png").read_bytes(), b"image bytes")
            self.assertFalse((restored / "lumiverse.db-wal").exists())
            with closing(sqlite3.connect(restored / "lumiverse.db")) as db:
                self.assertEqual(db.execute("SELECT message FROM chats").fetchone()[0], "after setup")

    def test_wrong_key_rejects_archive(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            plain = root / "plain"
            cipher = root / "cipher"
            result = root / "result"
            plain.write_bytes(b"private chat")
            backup.encrypt_file(plain, cipher, os.urandom(32))
            with self.assertRaises(Exception):
                backup.decrypt_file(cipher, result, os.urandom(32))

    def test_rejects_archive_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            archive_path = root / "bad.tar.gz"
            source = root / "source"
            source.write_bytes(b"bad")
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(source, arcname="../outside")
            dest = root / "dest"
            dest.mkdir()
            with self.assertRaisesRegex(RuntimeError, "Unsafe backup archive path"):
                backup.extract_safe(archive_path, dest)
            self.assertFalse((root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
