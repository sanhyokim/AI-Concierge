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


@unittest.skipUnless(sys.platform != "win32", "runs bash")
class MacStartFileTest(unittest.TestCase):
    """start_mac.command: executable in the repository (so in GitHub's ZIP) and its branches."""

    def run_start(self, path_dirs, script=None):
        env = {"PATH": os.pathsep.join(path_dirs), "START_CHECK_ONLY": "1"}
        return subprocess.run(["/bin/bash", str(script or ROOT / "start_mac.command")], input="\n", env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_executable_and_valid_bash(self):
        for name in ("start_mac.command", "reset_password_mac.command"):
            mode = subprocess.run(["git", "ls-files", "-s", name], cwd=ROOT, capture_output=True, text=True,
                                  encoding="utf-8").stdout.split()[0]
            self.assertEqual(mode, "100755", name)
            self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / name)]).returncode, 0, name)

    def test_branches(self):
        import shutil
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = pathlib.Path(tmp.name) / "bin"
        fake.mkdir()
        for tool in ("dirname", "uname"):
            (fake / tool).symlink_to(shutil.which(tool))
        r = self.run_start([str(fake)])                             # no Python at all
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.11 以上が見つかりません", r.stdout)
        old = fake / "python3"
        old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")      # a Python older than 3.11
        old.chmod(0o755)
        r = self.run_start([str(fake)])
        self.assertIn("Python 3.11 以上が見つかりません", r.stdout)
        old.unlink()
        (fake / "python3").symlink_to(sys.executable)               # a good one
        r = self.run_start([str(fake)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("python:", r.stdout)
        outside = pathlib.Path(tmp.name) / "start_mac.command"      # opened outside the extracted folder
        outside.write_bytes((ROOT / "start_mac.command").read_bytes())
        r = self.run_start([str(fake)], outside)
        self.assertIn("展開したフォルダーの中から", r.stdout)

    def test_certificate_check_uses_the_macs_own_certificates(self):
        import shutil
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = pathlib.Path(tmp.name) / "bin"
        fake.mkdir()
        for tool in ("dirname", "uname", "cat"):
            (fake / tool).symlink_to(shutil.which(tool))
        # a Python whose --check-https answers $FIRST, or $WITH_FILE when SSL_CERT_FILE is set
        (fake / "python3").write_text('#!/bin/bash\ncase "$*" in *--check-https*) '
                                      '[ -n "$SSL_CERT_FILE" ] && exit "$WITH_FILE"; exit "$FIRST";; esac\nexit 0\n',
                                      encoding="utf-8")
        security = fake / "security"
        security.write_text("#!/bin/bash\necho '-----BEGIN CERTIFICATE-----'\n", encoding="utf-8")
        for f in (fake / "python3", security):
            f.chmod(0o755)

        def run(first, with_file="0"):
            env = {"PATH": str(fake), "START_CHECK_ONLY": "https", "FIRST": first, "WITH_FILE": with_file,
                   "TMPDIR": tmp.name}
            return subprocess.run(["/bin/bash", str(ROOT / "start_mac.command")], input="\n", env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=30)

        r = run("0")
        self.assertIn("https: 0 \n", r.stdout + "\n")                     # fine as is: no certificate file
        r = run("2", "0")
        self.assertIn("Mac 本体の証明書を使います", r.stdout)
        self.assertIn("https: 0 cert-file", r.stdout)
        self.assertIn("BEGIN CERTIFICATE", (pathlib.Path(tmp.name) / "ai-concierge-root-certificates.pem")
                      .read_text(encoding="utf-8"))
        r = run("2", "2")                                                 # the Mac's certificates do not help
        self.assertIn("Install Certificates.command", r.stdout)
        self.assertIn("https: 2", r.stdout)                               # still starts (the offline mock works)
        security.unlink()
        r = run("2")
        self.assertIn("Install Certificates.command", r.stdout)
        r = run("3")
        self.assertIn("インターネットにつながっていない", r.stdout)
        self.assertIn("https: 3", r.stdout)


class HttpsCheckTest(unittest.TestCase):
    def test_classification(self):
        import socket
        import ssl
        said = []

        def fails(exc):
            def connect(host, timeout):
                raise exc
            return connect

        self.assertEqual(setup.check_https(say=said.append, connect=lambda h, t: None), setup.HTTPS_OK)
        cert = ssl.SSLCertVerificationError(1, "certificate verify failed")
        cert.verify_message = "unable to get local issuer certificate"
        self.assertEqual(setup.check_https(say=said.append, connect=fails(cert)), setup.HTTPS_CERTIFICATE)
        self.assertIn("unable to get local issuer certificate", said[-1])
        self.assertEqual(setup.check_https(say=said.append, connect=fails(socket.gaierror(8, "nodename"))),
                         setup.HTTPS_NETWORK)
        self.assertEqual(setup.check_https(say=said.append, connect=fails(TimeoutError())), setup.HTTPS_NETWORK)
