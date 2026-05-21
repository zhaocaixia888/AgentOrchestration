"""API middleware components."""

import time
import logging
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.common.auth_service import auth_service
from src.common.rbac import check_action_permission
from src.common.webhook_logger import redact_headers

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Authentication middleware with live token validation and trailing slash protection."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path.rstrip("/") or "/"
        if path.startswith("/api/v2") and path != "/api/v2/auth/token":
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return Response(status_code=401, content="Unauthorized")
            token = auth_header[7:]
            info = auth_service.validate_token(token)
            if info is None:
                return Response(status_code=401, content="Token revoked or invalid")
            request.state.user_id = info.get("user_id", "unknown")
            request.state.roles = info.get("roles", ["viewer"])
        return await call_next(request)


class RBACMiddleware(BaseHTTPMiddleware):
    """RBAC authorization middleware."""

    # Maps URL path prefixes to actions
    PATH_ACTIONS = {
        "/api/v2/agents": "agent:list",
        "/api/v2/agents/": "agent:get",
        "/api/v2/agents/count": "agent:list",
    }

    POST_PATH_ACTIONS = {
        "/api/v2/agents": "agent:create",
    }

    DELETE_PATH_ACTIONS = {
        "/api/v2/agents/": "agent:delete",
    }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path.rstrip("/") or "/"
        method = request.method

        action = None
        if method == "POST":
            for prefix, act in self.POST_PATH_ACTIONS.items():
                if path.startswith(prefix):
                    action = act
                    break
        elif method == "DELETE":
            for prefix, act in self.DELETE_PATH_ACTIONS.items():
                if path.startswith(prefix):
                    action = act
                    break
        else:
            for prefix, act in self.PATH_ACTIONS.items():
                if path.startswith(prefix):
                    action = act
                    break

        if action and hasattr(request.state, "roles"):
            roles = request.state.roles
            has_permission = any(
                check_action_permission(role, action) for role in (roles if isinstance(roles, list) else [roles])
            )
            if not has_permission:
                logger.warning(
                    f"RBAC denied: user={getattr(request.state, 'user_id', 'unknown')} "
                    f"action={action} path={path}"
                )
                return Response(status_code=403, content="Insufficient permissions")

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 100, window: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window
        self._requests = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        if client_ip not in self._requests:
            self._requests[client_ip] = []

        self._requests[client_ip] = [t for t in self._requests[client_ip] if now - t < self.window]

        if len(self._requests[client_ip]) >= self.max_requests:
            return Response(status_code=429, content="Too many requests")

        self._requests[client_ip].append(now)
        return await call_next(request)


class LoggingMiddleware(BaseHTTPMiddleware):
    MAX_CARDINALITY = 100

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        path_labels = len(request.url.path.split("/"))
        if path_labels > self.MAX_CARDINALITY:
            logger.warning(
                f"High metric cardinality detected: {path_labels} labels in path {request.url.path}"
            )
        safe_headers = redact_headers(dict(request.headers))
        logger.info(
            f"{request.method} {request.url.path} {response.status_code} "
            f"{duration:.3f}s user={getattr(request.state, 'user_id', 'anon')}"
        )
        return response
