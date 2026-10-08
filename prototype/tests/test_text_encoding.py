"""Every text file is read and written as UTF-8 explicitly.

Without encoding=, Python uses the system's default: cp932 on Japanese Windows, so the UTF-8 files of this
project cannot be read there (the first Windows start failed this way). Binary modes are exempt."""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]   # prototype/
NOT_FILES = {"wave", "webbrowser"}                   # wave.open(..., "rb"), webbrowser.open(url)


def implicit_encoding_calls(source: str) -> list[int]:
    """Line numbers of read_text / write_text / open calls in text mode, and subprocess calls with text=True,
    that do not name an encoding."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        if name in ("run", "Popen", "check_output"):   # subprocess output read as text
            if any(k.arg in ("text", "universal_newlines") and isinstance(k.value, ast.Constant) and k.value.value
                   for k in node.keywords):
                found.append(node.lineno)
            continue
        if name not in ("read_text", "write_text", "open"):
            continue
        if name == "open":
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in NOT_FILES:
                continue
            args = node.args[1:] if isinstance(f, ast.Name) else node.args   # open(path, mode) / path.open(mode)
            mode = next((k.value for k in node.keywords if k.arg == "mode"), args[0] if args else None)
            if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                continue
        found.append(node.lineno)
    return found


class TextEncodingTest(unittest.TestCase):
    def test_every_text_read_and_write_names_utf8(self):
        problems = []
        for path in sorted(ROOT.rglob("*.py")):
            for line in implicit_encoding_calls(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(ROOT.parent)}:{line}")
        self.assertEqual(problems, [], "add encoding=\"utf-8\" (Windows reads files as cp932 otherwise)")

    def test_the_check_finds_what_it_should(self):
        bad = ('import pathlib, subprocess\npathlib.Path("a").read_text()\nopen("b")\nopen("c", "w")\n'
               'pathlib.Path("d").open("a")\nsubprocess.run(["x"], capture_output=True, text=True)\n')
        self.assertEqual(implicit_encoding_calls(bad), [2, 3, 4, 5, 6])
        ok = ('import pathlib, wave, webbrowser\npathlib.Path("a").read_text(encoding="utf-8")\nopen("b", "rb")\n'
              'pathlib.Path("c").open("ab")\nwave.open("d", "rb")\nwebbrowser.open("http://x")\n'
              'open("e", mode="wb")\nopen("f", "w", encoding="utf-8")\n')
        self.assertEqual(implicit_encoding_calls(ok), [])


if __name__ == "__main__":
    unittest.main()
