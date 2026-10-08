"""The start for people who do not use a terminal: password and keys asked in the window, Windows start file."""
import os
import pathlib
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest

from prototype.admin import setup
from prototype.admin.auth import Auth
from prototype.reception.store import Store

ROOT = pathlib.Path(__file__).resolve().parents[2]
OPENAI = "sk-proj-" + "a" * 40
GEMINI = "AIza" + "b" * 35


def scripted(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


class SetupPromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "app.db")
        self.said = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_first_start_password_is_chosen_and_confirmed(self):
        auth = Auth(self.store)
        setup.ensure_password(self.store, auth, scripted("short", "goodpass1", "different", "goodpass1", "goodpass1"),
                              self.said.append)
        self.assertTrue(auth.login("admin", "goodpass1"))
        self.assertIn("8文字以上", "\n".join(self.said))
        self.assertIn("1回目と違います", "\n".join(self.said))
        # second start: nothing is asked
        setup.ensure_password(self.store, auth, scripted(), self.said.append)
        self.assertIn("最初の起動で決めたもの", self.said[-1])

    def test_giving_up_prints_a_generated_password_once(self):
        auth = Auth(self.store)
        setup.ensure_password(self.store, auth, scripted("", "", ""), self.said.append)
        m = re.search(r"自動で作りました：(\S+)", "\n".join(self.said))
        self.assertTrue(m and auth.login("admin", m.group(1)))

    def test_reset_password_logs_everyone_out(self):
        auth = Auth(self.store)
        auth.ensure_admin("oldpassword")
        token, _ = auth.login("admin", "oldpassword")
        self.assertTrue(setup.reset_password(self.store, scripted("newpassword", "newpassword"), self.said.append))
        self.assertIsNone(auth.session(token))
        self.assertIsNone(auth.login("admin", "oldpassword"))
        self.assertTrue(auth.login("admin", "newpassword"))

    def test_keys_are_checked_kept_in_memory_and_can_be_skipped(self):
        env = {}
        got = setup.ask_keys(env, scripted("  'sk-short' ", f' "{OPENAI}" ', ""), self.said.append)
        self.assertEqual(env, {"OPENAI_API_KEY": OPENAI})      # quotes and spaces removed; Gemini skipped
        self.assertEqual(got, {"OPENAI_API_KEY": "typed"})
        text = "\n".join(self.said)
        self.assertIn("形が違うようです", text)
        self.assertIn("sk- で始まる 48 文字", text)
        self.assertNotIn(OPENAI, text)                          # the key is never shown
        env2 = {"GEMINI_API_KEY": GEMINI}
        setup.ask_keys(env2, scripted("x", "y", "z"), self.said.append)
        self.assertNotIn("OPENAI_API_KEY", env2)                 # three wrong shapes: not set
        self.assertIn("設定済み", "\n".join(self.said))


class WindowsStartFileTest(unittest.TestCase):
    def test_start_files_are_crlf_and_their_labels_exist(self):
        for name in ("start_windows.bat", "reset_password_windows.bat"):
            raw = (ROOT / name).read_bytes()
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), name)          # CRLF only
            lines = raw.decode("utf-8").split("\r\n")
            self.assertEqual(lines[:2], ["@echo off", "chcp 65001 >nul"])   # UTF-8 before any Japanese
            self.assertTrue(all(ord(c) < 128 for c in lines[0] + lines[1]))
            labels = {l[1:] for l in lines if l.startswith(":")}
            for target in re.findall(r"goto (\w+)", raw.decode("utf-8")):
                self.assertIn(target, labels, f"{name}: goto {target}")
            self.assertIn("-m prototype.admin", raw.decode("utf-8"))
        self.assertIn("*.bat -text", (ROOT / ".gitattributes").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "prototype" / "admin" / "__main__.py").exists())


@unittest.skipUnless(sys.platform.startswith("linux"), "needs a pseudo-terminal")
class InteractiveStartTest(unittest.TestCase):
    """Runs `python -m prototype.admin --setup` on a real terminal and answers the prompts."""

    def test_setup_in_a_terminal_then_login(self):
        import pty
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "GEMINI_API_KEY")}
        env["BROWSER"] = "true"          # webbrowser runs `true` instead of opening a browser
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, "-m", "prototype.admin", "--setup", "--port", "0", "--db",
                                 str(d / "app.db"), "--ledger", str(d / "l.jsonl"), "--results-dir", str(d / "r")],
                                cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        self.addCleanup(os.close, master)
        self.addCleanup(lambda: (proc.kill(), proc.wait(5)))
        out = b""

        def until(pattern, timeout=20):
            nonlocal out
            end = time.time() + timeout
            while time.time() < end:
                if re.search(pattern.encode(), out):
                    return
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    try:
                        out += os.read(master, 4096)
                    except OSError:
                        break
            self.fail(f"did not see {pattern!r} in:\n{out.decode(errors='replace')}")

        for prompt, answer in (("パスワードを決めて入力", "pass-for-test-1"), ("同じパスワード", "pass-for-test-1"),
                               ("GPT-Live 1", OPENAI), ("Gemini 3.8 Live", "")):
            until(prompt)
            os.write(master, answer.encode() + b"\r")
        until("準備ができました")
        port = int(re.search(rb"http://127\.0\.0\.1:(\d+)", out).group(1))
        text = out.decode(errors="replace")
        self.assertNotIn("pass-for-test-1", text)                # typed secrets are not echoed
        self.assertNotIn(OPENAI, text)
        self.assertIn("受け付けました", text)
        import http.client, json
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/login", json.dumps({"username": "admin", "password": "pass-for-test-1"}),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        r.read()
        c.request("GET", "/lab/api/candidates", headers={"Cookie": cookie})
        cands = {x["id"]: x for x in json.loads(c.getresponse().read())["candidates"]}
        self.assertTrue(cands["gpt-live-1"]["ready"])            # the typed key reached the server process
        self.assertFalse(cands["gemini-3.8-live"]["ready"])      # skipped
        db = (d / "app.db").read_bytes()
        self.assertNotIn(OPENAI.encode(), db)                     # never written to disk


if __name__ == "__main__":
    unittest.main()
