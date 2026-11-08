import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import settings
from app import create_app
from conftest import make_engine
from engine import SYSTEM_PROMPT, EngineError
from options import SETTINGS
from tokens import TokenStore

BODY = {"text": "hello world bye now"}


def client(engine=None, admin_password=None, **options):
    return TestClient(create_app(engine or make_engine(), admin_password, **options))


def test_token_project_scopes_are_rejected_and_inherited_scopes_are_masked(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    inherited = store.create("legacy", ["old"], None, None)
    c = client(admin_password="admin", tokens=store)
    admin = {"X-Internal-Token": "admin"}
    for scope in ([], ["old"]):
        assert c.post("/admin/tokens", headers=admin, json={"name": "n", "projects": scope}).status_code == 422
        assert c.put(f"/admin/tokens/{inherited['id']}", headers=admin, json={"projects": scope}).status_code == 422
    assert c.get("/admin/tokens", headers=admin).json()["tokens"][0]["projects"] is None
    assert c.post("/admin/tokens", headers=admin, json={"name": "n", "projects": None}).status_code == 200


def test_each_project_gets_its_own_revocable_token(tmp_path):
    c = client(admin_password="admin", tokens=TokenStore(tmp_path / "t.json"))
    admin = {"X-Internal-Token": "admin"}
    assert c.post("/v1/normalize", json=BODY).status_code == 401
    made = c.post("/admin/tokens", json={"name": "Garrison"}, headers=admin).json()
    assert made["token"].startswith("s1mini_")
    use = {"X-Internal-Token": made["token"]}
    assert c.post("/v1/normalize", json=BODY, headers=use).status_code == 200
    assert c.delete(f"/admin/tokens/{made['id']}", headers=admin).status_code == 200
    assert c.post("/v1/normalize", json=BODY, headers=use).status_code == 401
    assert c.get("/health").status_code == 200  # /health needs no token


def test_rate_limit_edit_stats_and_metrics(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    c = client(admin_password="admin", tokens=store)
    admin = {"X-Internal-Token": "admin"}
    made = c.post("/admin/tokens", json={"name": "p", "rate": 1}, headers=admin).json()
    use = {"X-Internal-Token": made["token"]}
    assert c.post("/v1/normalize", json=BODY, headers=use).status_code == 200
    limited = c.post("/v1/normalize", json=BODY, headers=use)
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert c.put(f"/admin/tokens/{made['id']}", json={"rate": None}, headers=admin).status_code == 200
    assert c.post("/v1/normalize", json=BODY, headers=use).status_code == 200  # the limit is gone
    assert c.put("/admin/tokens/nope", json={}, headers=admin).status_code == 404
    assert store.list()[0]["timed"] >= 1
    assert c.get("/metrics").status_code == 401
    assert 's1mini_requests_total{token="p"}' in c.get("/metrics", headers=admin).text
    assert 's1mini_request_seconds_bucket{le="+Inf"}' in c.get("/metrics", headers=admin).text


def test_requests_over_the_in_flight_limit_get_429():
    async def main():
        engine = make_engine()
        release = asyncio.Event()
        real = engine.normalize

        async def slow(*args, **kwargs):
            await release.wait()
            return await real(*args, **kwargs)

        engine.normalize = slow
        app = create_app(engine, max_inflight=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc") as http:
            first = asyncio.create_task(http.post("/v1/normalize", json=BODY))
            await asyncio.sleep(0.2)
            second = await http.post("/v1/normalize", json=BODY)
            release.set()
            return second, await first

    second, first = asyncio.run(main())
    assert second.status_code == 429 and second.headers["Retry-After"] == "5"
    assert first.status_code == 200


def test_workers_are_read_only_and_admin_only(monkeypatch):
    monkeypatch.setenv("SERVICE_REPLICAS", "3")
    c = client(admin_password="admin")
    assert c.get("/admin/workers").status_code == 401
    assert c.get("/admin/workers", headers={"X-Internal-Token": "admin"}).json() == {"replicas": 3, "ollama_parallel": 1}


def test_num_thread_is_reported_and_set_by_the_admin(tmp_path):
    seen = []
    engine = make_engine()
    c = client(engine, "admin", tokens=TokenStore(tmp_path / "t.json"))
    admin = {"X-Internal-Token": "admin"}
    assert c.put("/admin/threads", json={"num_thread": 0}, headers=admin).status_code == 422
    assert c.put("/admin/threads", json={"num_thread": 3}, headers=admin).json() == {"num_thread": 3}
    assert seen == [] and c.get("/health").json()["num_thread"] == 3
    assert settings.load(tmp_path / "settings.json")["num_thread"] == 3


def test_settings_are_admin_only_checked_and_apply_live(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    config = settings.Config(store.settings_path, SETTINGS)
    config.set({"update_check_hours": 0, "warm": False})
    c = client(admin_password="admin", tokens=store, config=config)
    admin = {"X-Internal-Token": "admin"}
    assert c.get("/admin/settings").status_code == 401
    assert c.put("/admin/settings", json={"values": {"max_inflight": -1}}, headers=admin).status_code == 422
    assert c.put("/admin/settings", json={"values": {"max_inflight": 7}}, headers=admin).status_code == 200
    saved = {item["key"]: item for item in c.get("/admin/settings", headers=admin).json()["settings"]}
    assert saved["max_inflight"]["value"] == 7
    assert client(admin_password="admin").put("/admin/settings", json={"values": {}}, headers=admin).status_code == 409


def test_what_another_replica_saved_reaches_this_one(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    config = settings.Config(store.settings_path, SETTINGS)
    config.set({"update_check_hours": 0, "warm": False})
    seen = []
    engine = make_engine()
    with TestClient(create_app(engine, "admin", tokens=store, config=config)):
        settings.save(store.settings_path, num_thread=5, chunk_tokens=300)
        for _ in range(300):  # the loop looks every second
            if engine.chunk_tokens == 300:
                break
            time.sleep(0.02)
    assert seen == [] and engine.chunk_tokens == 300


def test_startup_warms_the_model():
    engine = make_engine()
    with TestClient(create_app(engine, warm=True)) as c:
        for _ in range(100):
            if c.get("/health").json()["ready"]:
                break
            time.sleep(0.02)
        assert engine.loads == ["m"] and c.get("/health").json()["ready"] is True


def test_health_answers_while_the_model_is_still_loading():
    import threading
    release = threading.Event()

    def slow(source):
        release.wait(5)
        return make_engine()._loader(source)

    with TestClient(create_app(make_engine(loader=slow), warm=True)) as c:
        assert c.get("/health").json()["ready"] is False
        release.set()


def test_warmup_failure_is_logged_without_stopping_service(caplog):
    def failing(source):
        raise RuntimeError("no weights")

    with TestClient(create_app(make_engine(loader=failing), warm=True)) as c:
        assert c.get("/health").status_code == 200
        for _ in range(100):
            if "warm-up failed" in caplog.text:
                break
            time.sleep(0.02)
    assert "warm-up failed" in caplog.text


def test_warmup_is_optional():
    engine = make_engine()
    with TestClient(create_app(engine)):
        assert engine.loads == []


def test_main_builds_the_engine_from_settings(monkeypatch, tmp_path):
    import app as module

    monkeypatch.setenv("SERVICE_TOKENS_FILE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("SERVICE_NUM_THREAD", "2")
    monkeypatch.setenv("SERVICE_PORT", "9999")
    monkeypatch.setattr(module.S1MiniEngine, "num_thread", property(lambda self: None, lambda self, count: seen.append(count)))
    seen, started = [], {}
    monkeypatch.setattr("uvicorn.Server.run", lambda self: started.update(app=self.config.app, host=self.config.host, port=self.config.port))
    module.main()
    assert started["port"] == 9999 and started["host"] == "127.0.0.1" and seen == [2]


def test_normalize_sends_the_prompt_and_reports_usage():
    engine = make_engine()
    c = client(engine)
    body = c.post("/v1/normalize", json={**BODY, "styling": "casual", "structure": "lists", "context": "email"}).json()
    assert body["content"] == "Hello world. Bye now." and body["finish_reason"] == "stop" and body["model"] == "m"
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 5} and "duration_ms" in body
    call = engine._base.calls[0]
    assert call["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert call["messages"][1]["content"] == "[Styling: casual] [Structure: lists] [Context: email]\nhello world bye now"
    assert call["max_new_tokens"] == int(1.3 * 4 + 32)
    c.post("/v1/normalize", json=BODY)
    assert engine._base.calls[1]["messages"][1]["content"].startswith("[Styling: semi-formal] [Structure: prose] [Context: general]\n")


@pytest.mark.parametrize("extra", [{"styling": "rude"}, {"structure": "table"}, {"context": "chat"}, {"messages": []},
                                   {"tools": []}, {"temperature": 0.5}, {"text": ""}])
def test_request_is_validated(extra):
    assert client().post("/v1/normalize", json={**BODY, **extra}).status_code == 422


def test_an_empty_result_is_valid():
    body = client(make_engine(reply="")).post("/v1/normalize", json=BODY)
    assert body.status_code == 200 and body.json()["content"] == ""


def test_long_input_is_cut_at_sentence_ends_and_joined():
    engine = make_engine(reply=lambda user: user.split("\n", 1)[1].upper(), chunk_tokens=3)
    body = client(engine).post("/v1/normalize", json={"text": "one two. three four. five six."}).json()
    assert body["content"] == "ONE TWO. THREE FOUR. FIVE SIX."
    assert [c["messages"][1]["content"].split("\n", 1)[1] for c in engine._base.calls] == ["one two.", "three four.", "five six."]
    assert body["usage"] == {"input_tokens": 9, "output_tokens": 15}


def test_a_model_crash_is_500_and_too_long_is_422():
    class Broken:
        parallel = 1
        async def normalize(self, *args):
            raise error

        async def close(self):
            pass

        model, num_thread, ready = "m", None, False

    for error, status in ((EngineError("too long", 400), 422), (RuntimeError("boom"), 500)):
        assert client(Broken()).post("/v1/normalize", json=BODY).status_code == status


def events(reply):
    return [json.loads(text) for text in reply.text.splitlines()]


def test_stream_sends_one_line_per_sentence_then_the_same_body_as_a_call_without_stream():
    engine = make_engine(reply="One two. Three four. Five")
    reply = client(engine).post("/v1/normalize", json={**BODY, "stream": True})
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("application/x-ndjson")
    sent = events(reply)
    assert [e["text"] for e in sent[:-1]] == ["One two. ", "Three four. ", "Five"]
    assert {e["type"] for e in sent[:-1]} == {"sentence"}
    done = sent[-1]
    assert done["type"] == "done" and done["content"] == "One two. Three four. Five" and done["finish_reason"] == "stop"
    assert done["usage"] == {"input_tokens": 3, "output_tokens": 5} and done["model"] == "m"


def test_stream_errors_before_the_first_sentence_are_http_errors_and_later_ones_are_error_lines():
    stream = {**BODY, "stream": True}
    assert client(make_engine(fail=EngineError("too long", 400))).post("/v1/normalize", json=stream).status_code == 422
    assert client(make_engine(fail=EngineError("broken", 500))).post("/v1/normalize", json=stream).status_code == 500
    cut = client(make_engine(reply="One two. Three four.", fail_after=3)).post("/v1/normalize", json=stream)
    assert cut.status_code == 200 and events(cut)[0] == {"type": "sentence", "text": "One two. "}
    assert events(cut)[-1] == {"type": "error", "status": 500, "message": "S1-mini could not answer"}


def test_the_same_request_is_answered_from_the_cache():
    engine = make_engine()
    c = client(engine, cache_size=4)
    first, second = c.post("/v1/normalize", json=BODY), c.post("/v1/normalize", json=BODY)
    assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("miss", "hit")
    assert second.json()["content"] == first.json()["content"] and len(engine._base.calls) == 1
    c.post("/v1/normalize", json={**BODY, "styling": "formal"})  # any change in the request is another answer
    c.post("/v1/normalize", json={"text": "other"})
    assert len(engine._base.calls) == 3
    off = client(make_engine())  # no cache_size: off
    assert [off.post("/v1/normalize", json=BODY).headers["X-Cache"] for _ in range(2)] == ["miss", "miss"]


def test_the_cache_is_not_used_after_the_weights_were_replaced():
    engine = make_engine()
    c = client(engine, cache_size=8)
    c.post("/v1/normalize", json=BODY)
    assert c.post("/v1/normalize", json=BODY).headers["X-Cache"] == "hit"
    asyncio.run(engine.unload())  # new weights
    assert c.post("/v1/normalize", json=BODY).headers["X-Cache"] == "miss"


def test_stream_fills_the_cache_and_a_kept_answer_streams_back_sentence_by_sentence():
    engine = make_engine(reply="One two. Three four.")
    c = client(engine, cache_size=4)
    streamed = c.post("/v1/normalize", json={**BODY, "stream": True})
    kept = c.post("/v1/normalize", json={**BODY, "stream": True})
    assert (streamed.headers["X-Cache"], kept.headers["X-Cache"]) == ("miss", "hit") and len(engine._base.calls) == 1
    sent = events(kept)
    assert [e["type"] for e in sent] == ["sentence", "sentence", "done"]
    assert [e["text"] for e in sent[:2]] == ["One two. ", "Three four."] and sent[2]["content"] == "One two. Three four."
    assert c.post("/v1/normalize", json=BODY).headers["X-Cache"] == "hit"  # the same cache serves a call without stream


def test_cache_size_is_a_setting_that_applies_live(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    config = settings.Config(store.settings_path, SETTINGS)
    config.set({"update_check_hours": 0, "warm": False})
    c = client(make_engine(), admin_password="admin", tokens=store, config=config)
    admin = {"X-Internal-Token": "admin"}
    use = {"X-Internal-Token": c.post("/admin/tokens", json={"name": "t"}, headers=admin).json()["token"]}
    assert [c.post("/v1/normalize", json=BODY, headers=use).headers["X-Cache"] for _ in range(2)] == ["miss", "hit"]
    assert c.put("/admin/settings", json={"values": {"cache_size": 0}}, headers=admin).status_code == 200
    assert c.post("/v1/normalize", json=BODY, headers=use).headers["X-Cache"] == "miss"
