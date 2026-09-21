"""Real-profile auth copy: a rollback-journal DB held under a persistent lock (macOS Chrome 153+)
must still copy quickly via an immutable read; a WAL-mode DB must keep the locking read."""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from hermes_cli.browser_connect import _copy_auth_file  # noqa: E402


def _make_db(path: str, wal: bool) -> None:
    with sqlite3.connect(path) as con:
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE cookies (host TEXT)")
        con.execute("INSERT INTO cookies VALUES ('github.com')")


def test_locked_rollback_journal_db_copies_fast():
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "Cookies"), os.path.join(d, "copy", "Cookies")
        _make_db(src, wal=False)
        holder = sqlite3.connect(src, isolation_level=None)
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("SELECT count(*) FROM cookies")  # persistent lock, like a running Chrome
        t0 = time.monotonic()
        assert _copy_auth_file(src, dst) is True
        assert time.monotonic() - t0 < 2.0
        assert sqlite3.connect(dst).execute("SELECT host FROM cookies").fetchone() == ("github.com",)
        holder.close()


def test_wal_db_keeps_locking_read():
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "Cookies"), os.path.join(d, "copy", "Cookies")
        _make_db(src, wal=True)
        assert os.path.exists(src + "-wal")
        assert _copy_auth_file(src, dst) is True  # unlocked WAL source still works
        assert sqlite3.connect(dst).execute("SELECT host FROM cookies").fetchone() == ("github.com",)


if __name__ == "__main__":
    test_locked_rollback_journal_db_copies_fast()
    test_wal_db_keeps_locking_read()
    print("ok")
