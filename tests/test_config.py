import pytest
from src.common.config import Config
from src.common.errors import ConfigurationError


class TestConfig:
    def test_load_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"app": {"name": "test", "port": 8080}}')
        config = Config(str(config_file))
        assert config.get("app.name") == "test"
        assert config.get("app.port") == 8080

    def test_default_value(self):
        config = Config()
        assert config.get("nonexistent.key", "default") == "default"

    def test_set_value(self):
        config = Config()
        config.set("database.host", "localhost")
        assert config.get("database.host") == "localhost"

    def test_nested_set(self):
        config = Config()
        config.set("a.b.c.d", "value")
        assert config.get("a.b.c.d") == "value"

    def test_to_dict(self):
        config = Config()
        config.set("key1", "value1")
        config.set("key2", "value2")
        data = config.to_dict()
        assert data["key1"] == "value1"
        assert data["key2"] == "value2"

    def test_oversized_config_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.common.config.MAX_CONFIG_SIZE", 100)
        config_file = tmp_path / "large.json"
        config_file.write_text('{"key": "' + "x" * 200 + '"}')
        with pytest.raises(ConfigurationError, match="Config file too large"):
            Config(str(config_file))

    def test_normal_config_accepted(self, tmp_path):
        config_file = tmp_path / "small.json"
        config_file.write_text('{"key": "value"}')
        config = Config(str(config_file))
        assert config.get("key") == "value"
