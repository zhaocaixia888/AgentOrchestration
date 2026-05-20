"""Tests for config numeric coercion."""
import os
from src.common.config import Config

class TestConfigNumericCoercion:
    def test_int_override(self):
        os.environ["AO_TEST_PORT"] = "8080"
        c = Config()
        val = c.get("test.port")
        assert isinstance(val, int), f"Expected int, got {type(val)}: {val}"
        assert val == 8080
        del os.environ["AO_TEST_PORT"]

    def test_float_override(self):
        os.environ["AO_TEST_RATE"] = "3.14"
        c = Config()
        val = c.get("test.rate")
        assert isinstance(val, float), f"Expected float, got {type(val)}: {val}"
        assert abs(val - 3.14) < 0.001
        del os.environ["AO_TEST_RATE"]

    def test_string_override(self):
        os.environ["AO_TEST_NAME"] = "hello"
        c = Config()
        val = c.get("test.name")
        assert isinstance(val, str)
        assert val == "hello"
        del os.environ["AO_TEST_NAME"]