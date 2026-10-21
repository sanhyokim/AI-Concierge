"""API keys kept on the Windows PC, encrypted for the Windows user (user decision 2026-10-20)."""
import pathlib
import tempfile
import unittest

from prototype.admin import keystore, setup

OPENAI = "sk-proj-" + "a" * 40
GEMINI = "AQ." + "b" * 36
ELEVEN = "sk_" + "e" * 48


def enc(data: bytes) -> bytes:      # stands in for DPAPI on this test machine
    return bytes(b ^ 0x5A for b in data)[::-1]


def dec(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data[::-1])


class KeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "data" / "api-keys.dpapi.json"

    def test_save_load_forget(self):
        keystore.save({"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI}, self.path, enc)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(OPENAI, raw)
        self.assertNotIn(GEMINI, raw)
        self.assertEqual(keystore.load(self.path, dec), {"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI})
        self.assertTrue(keystore.forget(self.path))
        self.assertEqual(keystore.load(self.path, dec), {})
        self.assertFalse(keystore.forget(self.path))

    def test_unreadable_file_is_no_saved_keys(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(keystore.load(self.path, dec), {})

        def other_user(_data):
            raise OSError("DPAPI error 13")   # another Windows user, or the file copied to another PC
        keystore.save({"OPENAI_API_KEY": OPENAI}, self.path, enc)
        self.assertEqual(keystore.load(self.path, other_user), {})

    def test_kept_next_to_the_records(self):
        # prototype/data is carried over by the automatic update (prototype/update.py KEEP)
        self.assertEqual(keystore.PATH.parent.name, "data")
        self.assertEqual(keystore.PATH.parent.parent.name, "prototype")
        self.assertFalse(keystore.available())   # this test machine is not Windows: nothing is stored


class AskKeysTest(unittest.TestCase):
    def run_ask(self, answers, saved=None, keep=None):
        said, asked = [], []

        def ask(prompt):
            asked.append(prompt)
            return answers.pop(0)
        env = {}
        got = setup.ask_keys(env, ask=ask, say=said.append, saved=saved, keep=keep)
        return env, got, asked, "\n".join(said)

    def test_typed_keys_are_saved_for_the_next_start(self):
        kept = []
        env, got, asked, said = self.run_ask([OPENAI, GEMINI, ELEVEN], keep=kept.append)
        self.assertEqual(kept, [{"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI, "ELEVENLABS_API_KEY": ELEVEN}])
        self.assertIn("暗号化して保存しました", said)
        self.assertIn("APIキーを入れ直す", said)
        self.assertNotIn(OPENAI, said)

    def test_saved_keys_are_used_without_asking(self):
        kept = []
        env, got, asked, said = self.run_ask([], saved={"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI,
                                                     "ELEVENLABS_API_KEY": ELEVEN}, keep=kept.append)
        self.assertEqual(asked, [])
        self.assertEqual(env, {"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI, "ELEVENLABS_API_KEY": ELEVEN})
        self.assertEqual(set(got.values()), {"saved"})
        self.assertEqual(kept, [])                       # nothing new to save
        self.assertIn("保存したキーを使います（sk- で始まる", said)
        self.assertNotIn(OPENAI, said)

    def test_a_missing_key_is_asked_and_saved_with_the_other(self):
        kept = []
        env, got, asked, said = self.run_ask([GEMINI], saved={"OPENAI_API_KEY": OPENAI, "ELEVENLABS_API_KEY": ELEVEN},
                                             keep=kept.append)
        self.assertEqual(len(asked), 1)
        self.assertEqual(kept, [{"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI, "ELEVENLABS_API_KEY": ELEVEN}])

    def test_a_key_added_later_is_asked_once_and_saved_with_the_saved_ones(self):
        # 2026-10-21: the ElevenLabs key was added after the user had saved the two others on Windows
        kept = []
        env, got, asked, said = self.run_ask([ELEVEN], saved={"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI},
                                             keep=kept.append)
        self.assertEqual(len(asked), 1)
        self.assertIn("ElevenLabs", asked[0])
        self.assertEqual(kept, [{"OPENAI_API_KEY": OPENAI, "GEMINI_API_KEY": GEMINI, "ELEVENLABS_API_KEY": ELEVEN}])
        self.assertNotIn(ELEVEN, said)

    def test_a_skipped_key_is_not_saved(self):
        kept = []
        env, got, asked, said = self.run_ask([OPENAI, "", ""], keep=kept.append)
        self.assertEqual(kept, [{"OPENAI_API_KEY": OPENAI}])

    def test_a_saved_key_of_the_wrong_shape_is_asked_again(self):
        env, got, asked, said = self.run_ask([OPENAI, GEMINI, ""], saved={"OPENAI_API_KEY": "broken"},
                                             keep=lambda k: None)
        self.assertEqual(len(asked), 3)

    def test_saving_fails_but_the_keys_work_for_this_start(self):
        def fail(_keys):
            raise OSError("DPAPI error 5")
        env, got, asked, said = self.run_ask([OPENAI, GEMINI, ""], keep=fail)
        self.assertEqual(env["OPENAI_API_KEY"], OPENAI)
        self.assertIn("保存できませんでした", said)

    def test_without_a_store_the_keys_stay_in_memory(self):     # Mac and other systems
        env, got, asked, said = self.run_ask([OPENAI, GEMINI, ""])
        self.assertIn("メモリーにだけ置き", said)
