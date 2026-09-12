"""The wake word decides when the microphone stops being furniture and starts
being an input, so the two things tested here are: it hears its own name in
the shapes a transcriber actually produces, and it refuses to run at all on a
metered backend — where every sentence spoken near the desk would be billed.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import data, voice  # noqa: E402


class WakeMatchTest(unittest.TestCase):
    def test_the_name_alone_wakes_with_no_command(self):
        woke, command = voice.wake_match("Aeris")
        self.assertTrue(woke)
        self.assertEqual(command, "")

    def test_name_and_command_in_one_breath(self):
        woke, command = voice.wake_match("Aeris, what's broken?")
        self.assertTrue(woke)
        self.assertEqual(command, "what's broken?")

    def test_a_greeting_before_the_name_still_wakes(self):
        woke, command = voice.wake_match("hey Aeris brief me")
        self.assertTrue(woke)
        self.assertEqual(command, "brief me")

    def test_common_mishearings_still_wake(self):
        """Being deaf to your own name is worse than waking once too often."""
        for heard in ("Aries plan my day", "Eris brief me", "arias what's next"):
            woke, _ = voice.wake_match(heard)
            self.assertTrue(woke, heard)

    def test_ordinary_speech_does_not_wake(self):
        for heard in ("tell me about Paris",
                      "what did I write about pricing",
                      "the series finale was good",
                      "open the iris diaphragm"):
            woke, _ = voice.wake_match(heard)
            self.assertFalse(woke, heard)

    def test_the_name_buried_mid_sentence_does_not_wake(self):
        """Talking *about* her is not talking *to* her."""
        woke, _ = voice.wake_match(
            "so I was telling him that Aeris can read my screen now")
        self.assertFalse(woke)

    def test_empty_audio_does_not_wake(self):
        self.assertEqual(voice.wake_match(""), (False, ""))
        self.assertEqual(voice.wake_match(None), (False, ""))

    def test_the_command_keeps_its_original_casing(self):
        _woke, command = voice.wake_match("Aeris, open the Scam Detection Platform")
        self.assertEqual(command, "open the Scam Detection Platform")


class VocabularyCorrectionTest(unittest.TestCase):
    """A transcript is the only thing routing ever sees, so a name the
    transcriber almost got is a question that reaches the wrong tool — or no
    tool at all. This pass fixes the close misses and, deliberately, nothing
    more: a wholly misheard word is a recording problem, and guessing at it
    from text would invent questions he never asked.
    """
    def test_a_near_miss_on_a_project_name_is_corrected(self):
        self.assertEqual(voice._correct_vocabulary("is railwey down"), "is railway down")

    def test_a_two_word_mishearing_is_rejoined(self):
        self.assertIn("github", voice._correct_vocabulary("what's failing on get hub").lower())

    def test_casing_survives_a_correction(self):
        self.assertEqual(voice._correct_vocabulary("Railwey has my deploys"),
                         "Railway has my deploys")

    def test_a_named_mishearing_of_an_english_adjacent_word_is_fixed(self):
        """"notion" cannot go in the fuzzy pass — it sits next to nation and
        notions — so its mishearings are named outright instead."""
        self.assertEqual(voice._correct_vocabulary("what is in notyon"),
                         "what is in notion")

    def test_ordinary_speech_is_left_alone(self):
        for line in ("what did I write about pricing",
                     "the client wants a call tomorrow",
                     "read me the last three emails",
                     "a nation of two hundred million",
                     "put it in motion this week",
                     "check my calendars and my notions",
                     "what am I working on"):
            self.assertEqual(voice._correct_vocabulary(line), line)

    def test_short_words_are_never_rewritten(self):
        """'ai', 'my', 'is' are within a letter or two of plenty of things."""
        self.assertEqual(voice._correct_vocabulary("is my ai on"), "is my ai on")

    def test_an_empty_transcript_stays_empty(self):
        self.assertEqual(voice._correct_vocabulary(""), "")
        self.assertIsNone(voice._correct_vocabulary(None))


class WakeReadinessTest(unittest.TestCase):
    def setUp(self):
        self._real = voice.stt_backend

    def tearDown(self):
        voice.stt_backend = self._real
        os.environ.pop("AERIS_WAKE_WORD", None)
        data.reload_env()

    def test_wake_refuses_to_run_on_a_metered_backend(self):
        """It would transcribe every overheard sentence, and bill for each."""
        voice.stt_backend = lambda: {"name": "elevenlabs", "ok": True, "free": False,
                                     "detail": "metered"}
        ready = voice.wake_ready()
        self.assertFalse(ready["ok"])
        self.assertEqual(ready["reason"], "metered")
        self.assertIn("whisper", ready["detail"].lower())

    def test_wake_runs_on_the_free_local_backend(self):
        voice.stt_backend = lambda: {"name": "whisper", "ok": True, "free": True,
                                     "detail": "local"}
        self.assertTrue(voice.wake_ready()["ok"])

    def test_wake_refuses_when_there_is_no_speech_to_text_at_all(self):
        voice.stt_backend = lambda: {"name": "none", "ok": False, "free": True,
                                     "detail": "nothing installed"}
        ready = voice.wake_ready()
        self.assertFalse(ready["ok"])
        self.assertEqual(ready["reason"], "no-stt")

    def test_an_overheard_chunk_that_was_not_for_her_returns_no_transcript(self):
        """What she overheard and discarded must not travel any further."""
        voice.stt_backend = lambda: {"name": "whisper", "ok": True, "free": True,
                                     "detail": ""}
        real_transcribe = voice.transcribe
        voice.transcribe = lambda audio, ctype="": ("my card number is 4111 1111", None)
        try:
            out, err = voice.hear_wake(b"x" * 2000)
        finally:
            voice.transcribe = real_transcribe
        self.assertIsNone(err)
        self.assertFalse(out["woke"])
        self.assertNotIn("4111", repr(out))

    def test_a_custom_wake_word_is_honoured(self):
        os.environ["AERIS_WAKE_WORD"] = "jarvis"
        data.reload_env()
        woke, command = voice.wake_match("Jarvis, brief me")
        self.assertTrue(woke)
        self.assertEqual(command, "brief me")


if __name__ == "__main__":
    unittest.main()
