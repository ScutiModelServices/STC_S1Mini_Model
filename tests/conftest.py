import pytest

from engine import S1MiniEngine


@pytest.fixture(autouse=True)
def _tmp_tokens_file(tmp_path, monkeypatch):
    """Keeps tokens.json and settings.json out of the repo, and a saved setting from leaking into the next test."""
    monkeypatch.setenv("SERVICE_TOKENS_FILE", str(tmp_path / "tokens.json"))


class FakeLoaded:
    """Stands in for a loaded model: answers `reply` (text, or a function of the user message) and records every call."""

    def __init__(self, source, reply="Hello world. Bye now.", fail=None, fail_after=None):
        self.source, self.reply, self.calls = source, reply, []
        self.fail, self.fail_after, self.stopped = fail, fail_after, None

    def count(self, text):
        return len(text.split())

    def stream(self, messages, max_new_tokens, max_time, stop):
        """The reply a word at a time, then the stats; `fail` raises before the first piece, `fail_after` after that many."""
        self.calls.append({"messages": messages, "max_new_tokens": max_new_tokens})
        self.stopped = stop
        if self.fail:
            raise self.fail
        reply = self.reply(messages[1]["content"]) if callable(self.reply) else self.reply
        words = reply.split(" ") if reply else []
        for number, word in enumerate(words):
            if stop.is_set():
                return
            if self.fail_after is not None and number >= self.fail_after:
                raise RuntimeError("the model crashed")
            yield "content", word + (" " if number < len(words) - 1 else "")
        yield "done", {"prompt_tokens": 3, "completion_tokens": 5, "finish": "stop"}


def make_engine(**options):
    """An engine whose loader builds FakeLoaded models; `engine.loads` lists the sources it was asked for."""
    loads = []
    reply = options.pop("reply", "Hello world. Bye now.")
    fail, fail_after = options.pop("fail", None), options.pop("fail_after", None)

    def loader(source):
        loads.append(source)
        return FakeLoaded(source, reply, fail, fail_after)

    options.setdefault("loader", loader)
    engine = S1MiniEngine("m", **options)
    engine.loads = loads
    return engine
