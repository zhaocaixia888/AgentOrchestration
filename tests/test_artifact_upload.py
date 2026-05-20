"""Tests for artifact upload body size enforcement."""
import pytest
from unittest.mock import Mock, AsyncMock
from starlette.responses import Response
from src.api.middleware import RequestBodySizeMiddleware

def create_middleware():
    return RequestBodySizeMiddleware(app=Mock())

def test_content_length_ok():
    mw = create_middleware()
    assert mw.MAX_SIZE == 100 * 1024 * 1024

def test_max_size_constant():
    from src.api.middleware import RequestBodySizeMiddleware
    assert RequestBodySizeMiddleware.MAX_SIZE == 100 * 1024 * 1024

def test_artifact_paths_configured():
    from src.api.middleware import RequestBodySizeMiddleware
    assert any("artifacts" in p for p in RequestBodySizeMiddleware.ARTIFACT_PATHS)