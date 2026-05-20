"""Tests for config branch protection."""
import pytest
from src.common.config import Config

def test_scalar_override_raises():
    c = Config()
    c._data = {"app": {"name": "test"}}
    with pytest.raises(ValueError, match="branch"):
        c._set_nested("app", "scalar")

def test_normal_set_works():
    c = Config()
    c._set_nested("app.name", "my-app")
    assert c._data["app"]["name"] == "my-app"

def test_nested_creation():
    c = Config()
    c._set_nested("a.b.c", 42)
    assert c._data["a"]["b"]["c"] == 42
