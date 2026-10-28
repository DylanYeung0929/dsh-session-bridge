#!/usr/bin/env python3
"""Offline unit tests for tools/dshtool.py — no DSH app, no ~/.dsh needed."""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import dshtool  # noqa: E402


def row(sid, title, mtime=0):
    return dict(id=sid, title=title, cwd="/tmp", usage={}, created=0,
                mtime=mtime, size=0, open=False, active=False, transcript=None)


ROWS = [
    row("session-aaaaaaaa-1111", "Alpha"),
    row("session-aaaaaaaa-2222", "Alpha two"),
    row("session-bbbbbbbb-3333", "Beta"),
]


class MsgContentTest(unittest.TestCase):
    def test_user_message_keeps_text_in_data_content(self):
        data = {"content": [{"type": "text", "text": "hello"}]}
        self.assertEqual(dshtool.text_of(dshtool.msg_content(data)), "hello")

    def test_assistant_message_wraps_text_in_data_message(self):
        data = {"message": {"content": [{"type": "text", "text": "hi there"}]}}
        self.assertEqual(dshtool.text_of(dshtool.msg_content(data)), "hi there")

    def test_message_wins_when_both_shapes_present(self):
        data = {"content": [{"type": "text", "text": "outer"}],
                "message": {"content": [{"type": "text", "text": "inner"}]}}
        self.assertEqual(dshtool.text_of(dshtool.msg_content(data)), "inner")

    def test_text_of_marks_images_and_skips_reasoning(self):
        content = [{"type": "text", "text": "look"},
                   {"type": "image", "url": "x"},
                   {"type": "reasoning", "text": "secret"}]
        self.assertEqual(dshtool.text_of(content), "look\n[图片]")

    def test_text_of_accepts_plain_string(self):
        self.assertEqual(dshtool.text_of("plain"), "plain")


class ResolveTest(unittest.TestCase):
    def test_exact_id(self):
        self.assertEqual(dshtool.resolve("session-bbbbbbbb-3333", ROWS)["title"], "Beta")

    def test_unique_id_prefix(self):
        self.assertEqual(dshtool.resolve("session-bbbb", ROWS)["title"], "Beta")

    def test_exact_title_beats_substring(self):
        self.assertEqual(dshtool.resolve("Alpha", ROWS)["title"], "Alpha")

    def test_ambiguous_prefix_raises_with_candidates(self):
        with self.assertRaises(dshtool.AmbiguousSession) as ctx:
            dshtool.resolve("session-aaaa", ROWS)
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_ambiguous_title_raises(self):
        with self.assertRaises(dshtool.AmbiguousSession):
            dshtool.resolve("Alph", ROWS)

    def test_unknown_needle_returns_none(self):
        self.assertIsNone(dshtool.resolve("nope", ROWS))


class LockHeldTest(unittest.TestCase):
    """session.lock semantics: held by another process => open in a client."""

    @staticmethod
    def _holder(path):
        code = textwrap.dedent("""
            import fcntl, os, sys, time
            fd = os.open(sys.argv[1], os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("locked", flush=True)
            time.sleep(60)
        """)
        return subprocess.Popen([sys.executable, "-c", code, path],
                                stdout=subprocess.PIPE, text=True)

    def test_detects_and_releases(self):
        with tempfile.NamedTemporaryFile() as fh:
            child = self._holder(fh.name)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                self.assertTrue(dshtool.lock_held(fh.name))
            finally:
                child.kill()
                child.wait()
                child.stdout.close()
            deadline = time.time() + 5
            while dshtool.lock_held(fh.name) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(dshtool.lock_held(fh.name))

    def test_missing_file_is_not_held(self):
        self.assertFalse(dshtool.lock_held("/nonexistent/session.lock"))


if __name__ == "__main__":
    unittest.main()
