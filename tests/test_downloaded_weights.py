"""Pinned tokenizer loads never reach the network."""
import sys
import types
import pytest
from ollama_backend import load_tokenizer, TOKENIZER_REVISION


def test_tokenizer_lookup_is_local_only(monkeypatch):
    def lookup(source, filename, **kwargs):
        assert kwargs == {"revision": TOKENIZER_REVISION, "token": False, "local_files_only": True}
        raise FileNotFoundError("not cached")
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=lookup))
    with pytest.raises(FileNotFoundError):
        load_tokenizer("superwhisper/s1-mini")


def test_local_import_downloads_only_tokenizer_and_recovers(monkeypatch, tmp_path):
    import io
    from fastapi.testclient import TestClient
    from app import create_app
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from ollama_backend import engine_from_env

    filename = tmp_path / "tokenizer.json"
    Tokenizer(WordLevel({"[UNK]":0}, unk_token="[UNK]")).save(str(filename))
    downloaded = False
    downloads = []
    def vocabulary(source, filename, **kwargs):
        nonlocal downloaded
        if kwargs.get("local_files_only"):
            if not downloaded:
                raise FileNotFoundError("not downloaded")
        else:
            downloads.append(kwargs)
            downloaded = True
        return str(tmp_path / "tokenizer.json")
    def call(url, path, body, timeout):
        assert path != "/api/pull", "local imports cannot be pulled"
        if path == "/api/tags":
            return io.BytesIO(b'{"models":[{"name":"s1-mini:f16","digest":"local"}]}')
        return io.BytesIO(b'{"done":true}')
    monkeypatch.setattr("huggingface_hub.hf_hub_download", vocabulary)
    monkeypatch.setattr("ollama_backend._call", call)
    monkeypatch.delenv("SERVICE_OLLAMA_MODEL", raising=False)
    engine = engine_from_env()
    with TestClient(create_app(engine, warm=True, admin_password="admin")) as http:
        headers = {"X-Internal-Token":"admin"}
        token = http.post("/admin/tokens", headers=headers, json={"name":"test"}).json()["token"]
        failed = http.post("/v1/normalize", json={"text":"hello"}, headers={"X-Internal-Token":token})
        assert failed.status_code == 503 and "Download" in failed.json()["detail"]
        assert http.get("/health").json()["ready"] is False and downloads == []
        check = http.post("/admin/updates/check", headers=headers).json()
        assert check["weights"][0]["latest"] is None and downloads == []
        assert http.post("/admin/updates/weights", headers=headers).status_code == 200
        assert len(downloads) == 1 and downloads[0]["revision"] == TOKENIZER_REVISION
        import asyncio
        asyncio.run(engine.warm())
        assert http.get("/health").json()["ready"] is True
