#!/usr/bin/env python3
"""Offline unit tests for tools/dshctl.py — no app, no network, no ~/.dsh."""
import contextlib
import io
import os
import sys
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import dshctl  # noqa: E402


class CookieHeaderTest(unittest.TestCase):
    def test_app_backend_sends_the_gui_cookie(self):
        app = SimpleNamespace(backend="app", name="dsh-auth-abc", value="v1.x.y")
        self.assertEqual(dshctl._cookie_header(app), "dsh-auth-abc=v1.x.y")

    def test_helper_backend_joins_the_jar(self):
        helper = SimpleNamespace(backend="helper", jar=[
            SimpleNamespace(name="dsh-client", value="one"),
            SimpleNamespace(name="other", value="two"),
        ])
        self.assertEqual(dshctl._cookie_header(helper), "dsh-client=one; other=two")


class AssistStreamTest(unittest.TestCase):
    """`_on_stream` renders assistant-stream frames without a terminal."""

    def render(self, frames, reasoning=False):
        args = SimpleNamespace(reasoning=reasoning)
        state = {"streamed": False, "prefix": False}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for frame in frames:
                dshctl._on_stream(frame, args, state)
        return buf.getvalue(), state

    @staticmethod
    def text_delta(text):
        return {"type": "chunk", "chunk": {"type": "text-delta", "index": 0, "text": text}}

    def test_start_deltas_end(self):
        out, state = self.render([
            {"type": "start"},
            self.text_delta("Hel"),
            self.text_delta("lo"),
            {"type": "end"},
        ])
        self.assertEqual(out, "\n它 > Hello\n")
        self.assertFalse(state["streamed"])
        self.assertFalse(state["prefix"])

    def test_delta_without_start_still_gets_a_prefix(self):
        out, _ = self.render([self.text_delta("hi"), {"type": "end"}])
        self.assertEqual(out, "\n它 > hi\n")

    def test_end_without_content_prints_nothing(self):
        out, _ = self.render([{"type": "start"}, {"type": "end"}])
        self.assertEqual(out, "\n它 > \n")

    def test_reasoning_hidden_by_default(self):
        frame = {"type": "chunk", "chunk": {"type": "reasoning-delta", "index": 0, "text": "思考"}}
        out, _ = self.render([frame])
        self.assertEqual(out, "")

    def test_reasoning_opt_in(self):
        frame = {"type": "chunk", "chunk": {"type": "reasoning-delta", "index": 0, "text": "思考"}}
        out, _ = self.render([frame], reasoning=True)
        self.assertEqual(out, "思考")


class WatchContractTest(unittest.TestCase):
    def test_roles_cover_both_message_events(self):
        self.assertEqual(set(dshctl.ROLE), {"user/message", "assistant/message"})

    def test_watch_is_registered_with_follow_flags(self):
        # argparse is built inside main(); inspect the source of truth instead of running it.
        with open(os.path.join(HERE, "..", "tools", "dshctl.py"), encoding="utf-8") as fh:
            src = fh.read()
        for needle in ('sub.add_parser("watch"', "session/follow", "/api/remote.mux",
                       "--no-stream", "--reasoning"):
            self.assertIn(needle, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
