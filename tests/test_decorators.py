"""Tests for task decorator timeout validation."""
import pytest
from src.sdk.decorators import task

class TestTaskDecoratorValidation:
    def test_valid_timeout(self):
        @task(timeout=30)
        async def dummy(): pass
        assert dummy.__task_config__["timeout"] == 30

    def test_zero_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout"):
            @task(timeout=0)
            async def dummy(): pass

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout"):
            @task(timeout=-1)
            async def dummy(): pass

    def test_default_timeout_valid(self):
        @task()
        async def dummy(): pass
        assert dummy.__task_config__["timeout"] == 300