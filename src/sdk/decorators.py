"""SDK decorators for agent definitions."""

import functools
import asyncio
import re
from typing import Any, Callable, Dict, Optional


_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def task(name: Optional[str] = None, retries: int = 0, timeout: int = 300):
    """Decorator for marking a method as an agent task handler."""
    def decorator(func: Callable) -> Callable:
        func.__task_config__ = {
            "name": name or func.__name__,
            "retries": retries,
            "timeout": timeout,
        }

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                result = await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=timeout,
                )
                return result
            except asyncio.TimeoutError:
                raise TimeoutError(f"Task {name or func.__name__} timed out after {timeout}s")

        return wrapper
    return decorator


def agent(name: str, version: str = "1.0.0", description: str = ""):
    """Decorator for marking a class as an agent definition."""
    if not _VERSION_PATTERN.match(version):
        raise ValueError(
            f"Invalid version string: '{version}'. "
            f"Expected semantic version format (e.g. '1.0.0')"
        )

    def decorator(cls: type) -> type:
        cls.__agent_config__ = {
            "name": name,
            "version": version,
            "description": description,
        }
        return cls
    return decorator


def on_event(event_type: str):
    """Decorator for marking a method as an event handler."""
    def decorator(func: Callable) -> Callable:
        func.__event_handler__ = event_type

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await func(*args, **kwargs)

        return wrapper
    return decorator
