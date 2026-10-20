"""prototype/update.py: Windows keeps the program in one folder and updates it by itself (2026-10-20)."""
import io
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from prototype import update

ROOT = pathlib.Path(__file__).resolve().parents[2]
UTF8_PATHS = sys.getfilesystemencoding().lower().replace("-", "") == "utf8"   # Windows: always (wide-char names)
OLD, NEW = "a" * 40, "b" * 40


def program(folder: pathlib.Path, version: str) -> pathlib.Path:
    (folder / "prototype" / "admin").mkdir(parents=True)
    (folder / "prototype" / "results").mkdir()
    (folder / "start_windows.bat").write_bytes(b"@echo off\r\n")
    (folder / "prototype" / "admin" / "__main__.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
    (folder / "prototype" / "update.py").write_bytes((ROOT / "prototype" / "update.py").read_bytes())
    (folder / "prototype" / "results" / "offline-sample.json").write_text("{}", encoding="utf-8")
    return folder


def zipped(sha: str, version: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        root = program(pathlib.Path(tmp) / f"AI-Concierge-{sha}", version)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in root.rglob("*"):
                z.write(f, f.relative_to(root.parent).as_posix())
        return buf.getvalue()


class GitHub:
    """Answers the commit query and the ZIP download; error=OSError for no network."""

    def __init__(self, sha=NEW, version="new", error=None):
        self.sha, self.version, self.error, self.urls = sha, version, error, []

    def __call__(self, url, accept=None, timeout=0):
        self.urls.append(url)
        if self.error:
            raise self.error
        if url == update.SHA_URL:
            return self.sha.encode()
        assert url == update.ZIP_URL.format(sha=self.sha), url
        return zipped(self.sha, self.version)


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.app, self.tools, self.desktop = base / "AI-Concierge", base / ".ai-concierge", base / "Desktop"
        self.desktop.mkdir()
        env = {"AI_CONCIERGE_HOME": str(self.app), "AI_CONCIERGE_TOOLS": str(self.tools),
               "AI_CONCIERGE_DESKTOP": str(self.desktop)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        # the folder the user extracted and has been using: records, password and results inside
        self.extracted = program(base / "Downloads" / "AI-Concierge-claude-new-session-w4sjel", "extracted")
        (self.extracted / "prototype" / "data").mkdir()
        (self.extracted / "prototype" / "data" / "app.db").write_text("records and password", encoding="utf-8")
        (self.extracted / "prototype" / "results" / "browser-lab").mkdir()
        (self.extracted / "prototype" / "results" / "browser-lab" / "c1.json").write_text("C1", encoding="utf-8")

    def version(self):
        return (self.app / "prototype" / "admin" / "__main__.py").read_text(encoding="utf-8")

    def test_first_install_takes_the_newest_version_and_the_users_records(self):
        gh = GitHub()
        self.assertEqual(update.run(["--install"], gh, here=self.extracted), 0)
        self.assertIn("new", self.version())
        self.assertEqual((self.app / "prototype" / "data" / "app.db").read_text(encoding="utf-8"), "records and password")
        self.assertTrue((self.app / "prototype" / "results" / "browser-lab" / "c1.json").is_file())
        self.assertTrue((self.app / "prototype" / "results" / "offline-sample.json").is_file())   # the download's own
        self.assertEqual(update.installed_sha(self.app), NEW)
        launcher = (self.desktop / update.LAUNCHER).read_bytes()
        self.assertIn(b"\r\n", launcher)
        self.assertNotIn(b"\n", launcher.replace(b"\r\n", b""))                     # CRLF only (cmd.exe)
        text = launcher.decode("utf-8")
        self.assertIn(str(self.tools / "update.py"), text)
        self.assertIn(str(self.app) + "\\start_windows.bat", text)
        line = next(l for l in text.splitlines() if "update.py" in l and "call" in l)
        self.assertTrue(line.rstrip().endswith("& exit /b"))   # the update may rewrite this file: nothing after it
        self.assertEqual((self.tools / "update.py").read_bytes(), (ROOT / "prototype" / "update.py").read_bytes())
        self.assertFalse(self.app.with_name("AI-Concierge.new").exists())
        self.assertFalse(self.app.with_name("AI-Concierge.old").exists())

    def test_later_updates_keep_records_and_skip_when_nothing_is_new(self):
        update.run(["--install"], GitHub(sha=OLD, version="old"), here=self.extracted)
        (self.app / "prototype" / "data" / "app.db").write_text("newer records", encoding="utf-8")
        gh = GitHub(sha=NEW, version="new")
        self.assertEqual(update.run([], gh), 0)
        self.assertIn("new", self.version())
        self.assertEqual((self.app / "prototype" / "data" / "app.db").read_text(encoding="utf-8"), "newer records")
        self.assertTrue((self.app / "prototype" / "results" / "browser-lab" / "c1.json").is_file())
        same = GitHub(sha=NEW)
        self.assertEqual(update.run([], same), 0)
        self.assertEqual(same.urls, [update.SHA_URL])                     # no download when already the newest

    def test_without_a_network_the_installed_version_starts(self):
        update.run(["--install"], GitHub(), here=self.extracted)
        self.assertEqual(update.run([], GitHub(error=OSError("offline"))), 0)
        self.assertIn("new", self.version())

    def test_first_install_without_a_network_uses_the_extracted_folder(self):
        self.assertEqual(update.run(["--install"], GitHub(error=OSError("offline")), here=self.extracted), 0)
        self.assertIn("extracted", self.version())
        self.assertTrue((self.app / "prototype" / "data" / "app.db").is_file())
        self.assertEqual(update.installed_sha(self.app), "")        # the next start downloads the newest

    def test_nothing_installed_and_no_network(self):
        self.assertEqual(update.run([], GitHub(error=OSError("offline"))), 1)

    def test_an_odd_answer_from_github_changes_nothing(self):
        update.run(["--install"], GitHub(), here=self.extracted)
        self.assertEqual(update.run([], GitHub(sha="<html>rate limited</html>")), 0)
        self.assertIn("new", self.version())


class FilesTest(unittest.TestCase):
    @unittest.skipUnless(UTF8_PATHS, "the file has a Japanese name")
    def test_install_launcher_in_the_zip(self):
        bat = (ROOT / "AI受付を起動_windows.bat").read_bytes()
        self.assertNotIn(b"\n", bat.replace(b"\r\n", b""))
        text = bat.decode("utf-8")
        self.assertIn("prototype\\update.py --install", text)
        line = next(l for l in text.splitlines() if "--install" in l)
        self.assertTrue(line.rstrip().endswith("& exit /b"))

    def test_update_imports_nothing_from_the_program(self):
        # the desktop file runs a copy of update.py from outside the program folder
        src = (ROOT / "prototype" / "update.py").read_text(encoding="utf-8")
        self.assertNotIn("from .", src)
        self.assertNotIn("import prototype", src)
