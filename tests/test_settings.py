import pytest

import settings
from settings import Config, Setting

SCHEMA = [
    Setting("hours", "Hours", "float", 6.0, "T_HOURS", 0, 24),
    Setting("size", "Size", "int", 3, "T_SIZE", 0, 10),
    Setting("on", "On", "bool", True, "T_ON"),
    Setting("name", "Name", "text", "x", "T_NAME", restart=True, blank=False),
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for spec in SCHEMA:
        monkeypatch.delenv(spec.env, raising=False)


def test_saved_beats_environment_beats_default(tmp_path, monkeypatch):
    config = Config(tmp_path / "s.json", SCHEMA)
    assert config.get("size") == 3
    monkeypatch.setenv("T_SIZE", "5")
    assert config.get("size") == 5
    config.set({"size": 7})
    assert config.get("size") == 7
    config.set({"size": None})
    assert config.get("size") == 5
    monkeypatch.setenv("T_SIZE", "junk")
    assert config.get("size") == 3  # a bad environment value is ignored
    monkeypatch.setenv("T_ON", "0")
    assert config.get("on") is False
    config.set({"on": True})
    assert config.get("on") is True


def test_values_are_checked_and_nothing_is_saved_when_one_is_bad(tmp_path):
    config = Config(tmp_path / "s.json", SCHEMA)
    for bad in ({"nope": 1}, {"size": 11}, {"size": 2.5}, {"size": True}, {"size": "4"}, {"on": "yes"}, {"name": 3},
                {"name": "  "}, {"hours": -1}):
        with pytest.raises(ValueError):
            config.set(bad)
    with pytest.raises(ValueError):
        config.set({"size": 4, "nope": 1})
    assert config.get("size") == 3
    config.set({"size": 4.0, "name": " y "})
    assert config.get("size") == 4 and config.get("name") == "y"


def test_other_replicas_see_a_saved_change(tmp_path):
    path = tmp_path / "s.json"
    one, two = Config(path, SCHEMA), Config(path, SCHEMA)
    assert two.get("size") == 3
    one.set({"size": 9})
    assert two.get("size") == 9


def test_describe_lists_source_and_limits(tmp_path, monkeypatch):
    config = Config(tmp_path / "s.json", SCHEMA)
    config.set({"hours": 2})
    monkeypatch.setenv("T_SIZE", "4")
    rows = {row["key"]: row for row in config.describe()}
    assert (rows["hours"]["source"], rows["size"]["source"], rows["on"]["source"]) == ("saved", "environment", "default")
    assert rows["name"]["restart"] is True and rows["hours"]["maximum"] == 24 and rows["size"]["value"] == 4


def test_other_saved_keys_survive(tmp_path):
    path = tmp_path / "s.json"
    settings.save(path, num_thread=7)
    Config(path, SCHEMA).set({"size": 1})
    assert settings.load(path) == {"num_thread": 7, "size": 1}
