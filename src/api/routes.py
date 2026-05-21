"""API route definitions 鈥?with RBAC, template cloning, webhook endpoints, and auth."""

from fastapi import APIRouter, HTTPException, Depends, Request
from typing import List, Dict, Optional

from src.agent import AgentRegistry, AgentStatus
from src.common.auth_service import auth_service
from src.common.rbac import workspace_role_manager, check_action_permission, Role
from src.common.webhook_logger import webhook_delivery_logger, redact_webhook_payload
from src.common.storage import manifest_checker, legal_hold_manager, retention_policy

router = APIRouter()
registry = AgentRegistry()


# ---- Helper to extract user info from request -----------------------------

def _get_user_role(request: Request) -> str:
    """Extract the user's primary role from the request."""
    roles = getattr(request.state, "roles", ["viewer"])
    return roles[0] if isinstance(roles, list) else roles


def _get_user_id(request: Request) -> str:
    return getattr(request.state, "user_id", "anonymous")



def _validate_webhook_url(url: str) -> None:
    """Validate webhook URL requires TLS in production."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("https",):
        raise HTTPException(
            status_code=400,
            detail=f"Webhook URL must use HTTPS in production, got: {parsed.scheme}"
        )


def _check_rbac(request: Request, action: str) -> str:
    """Check RBAC and return user_role. Raises HTTPException on failure."""
    user_role = _get_user_role(request)
    if not check_action_permission(user_role, action):
        raise HTTPException(
            status_code=403,
            detail=f"Insufficient permissions for action: {action}"
        )
    return user_role


# ---- Auth endpoints -------------------------------------------------------

@router.post("/auth/token")
async def create_token(user_id: str, roles: Optional[List[str]] = None):
    token, expiry = auth_service.issue_token(user_id, roles or ["viewer"])
    return {"token": token, "expiry": expiry, "token_type": "Bearer"}


@router.post("/auth/revoke")
async def revoke_token(token_hash: str):
    if auth_service.revoke_token(token_hash):
        return {"status": "revoked"}
    raise HTTPException(status_code=404, detail="Token not found")


@router.get("/auth/validate")
async def validate_token(request: Request):
    """Revalidate the current token (for long-poll health checks)."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = auth_header[7:]
    info = auth_service.validate_token(token)
    if info is None:
        raise HTTPException(status_code=401, detail="Token revoked or invalid")
    return {"valid": True, "user_id": info["user_id"], "roles": info["roles"]}


# ---- Agent endpoints ------------------------------------------------------

@router.get("/agents")
async def list_agents(request: Request, status: Optional[str] = None, group: Optional[str] = None):
    _check_rbac(request, "agent:list")
    status_filter = AgentStatus(status) if status else None
    return {"agents": registry.list(status=status_filter, group=group)}


@router.post("/agents")
async def register_agent(request: Request, name: str, agent_type: str,
                          config: Optional[Dict] = None):
    _check_rbac(request, "agent:create")
    agent_id = registry.register(name, agent_type, config)
    return {"agent_id": agent_id, "status": "registered"}


@router.get("/agents/{agent_id}")
async def get_agent(request: Request, agent_id: str):
    _check_rbac(request, "agent:get")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.delete("/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str):
    _check_rbac(request, "agent:delete")
    if not registry.delete(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "deleted"}


@router.post("/agents/{agent_id}/start")
async def start_agent(request: Request, agent_id: str):
    _check_rbac(request, "agent:start")
    if not registry.update_status(agent_id, AgentStatus.RUNNING):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "started"}


@router.post("/agents/{agent_id}/stop")
async def stop_agent(request: Request, agent_id: str):
    _check_rbac(request, "agent:stop")
    if not registry.update_status(agent_id, AgentStatus.PAUSED):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "stopped"}


@router.get("/agents/count")
async def agent_count():
    return {"count": registry.count()}


# ---- Template endpoints (Issue #552) --------------------------------------

# In-memory template store
_templates: Dict[str, Dict] = {}
_template_id_counter = 0


def _next_template_id() -> str:
    global _template_id_counter
    _template_id_counter += 1
    return f"tmpl-{_template_id_counter}"


@router.get("/templates")
async def list_templates(request: Request, group: Optional[str] = None):
    _check_rbac(request, "template:list")
    if group:
        return {"templates": [t for t in _templates.values() if t.get("group") == group]}
    return {"templates": list(_templates.values())}


@router.post("/templates")
async def create_template(request: Request, name: str, group: str = "default",
                           config: Optional[Dict] = None,
                           workspace_id: str = "default"):
    user_role = _check_rbac(request, "template:create")
    # Workspace-level RBAC
    user_id = _get_user_id(request)
    if not workspace_role_manager.can_perform(workspace_id, user_id, "template:create"):
        raise HTTPException(status_code=403, detail="Insufficient workspace permissions")

    tmpl_id = _next_template_id()
    _templates[tmpl_id] = {
        "id": tmpl_id,
        "name": name,
        "group": group,
        "config": config or {},
        "workspace_id": workspace_id,
        "created_by": user_id,
        "version": "1.0.0",
    }
    return {"template_id": tmpl_id, "status": "created"}


@router.post("/templates/clone")
async def clone_template(request: Request, template_id: str, new_name: str,
                          target_workspace_id: str = "default"):
    """Clone a template from one workspace to another.

    Fixes #552: Requires at least 'editor' role in the SOURCE workspace.
    """
    user_role = _check_rbac(request, "template:clone")
    user_id = _get_user_id(request)

    if template_id not in _templates:
        raise HTTPException(status_code=404, detail="Template not found")

    source = _templates[template_id]
    source_workspace = source.get("workspace_id", "default")

    # Enforce role check on source workspace (#552)
    if not workspace_role_manager.can_clone_template(source_workspace, user_id):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user_role}' insufficient to clone template from "
                   f"workspace '{source_workspace}'. Requires at least 'editor'."
        )

    cloned_id = _next_template_id()
    _templates[cloned_id] = {
        **source,
        "id": cloned_id,
        "name": new_name,
        "workspace_id": target_workspace_id,
        "cloned_from": template_id,
        "cloned_by": user_id,
        "version": "1.0.0",
    }
    return {"template_id": cloned_id, "status": "cloned", "source": template_id}


@router.get("/templates/{template_id}")
async def get_template(request: Request, template_id: str):
    _check_rbac(request, "template:get")
    tmpl = _templates.get(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tmpl


@router.delete("/templates/{template_id}")
async def delete_template(request: Request, template_id: str):
    _check_rbac(request, "template:delete")
    if template_id not in _templates:
        raise HTTPException(status_code=404, detail="Template not found")
    del _templates[template_id]
    return {"status": "deleted"}


# ---- Webhook endpoints (Issue #590) ---------------------------------------

_webhooks: Dict[str, Dict] = {}
_webhook_counter = 0
_webhook_delivery_log: List[Dict] = []


def _next_webhook_id() -> str:
    global _webhook_counter
    _webhook_counter += 1
    return f"wh-{_webhook_counter}"


@router.post("/webhooks")
async def register_webhook(request: Request, url: str, secret: Optional[str] = None,
                            events: List[str] = ["*"]):
    _check_rbac(request, "webhook:create")
    wh_id = _next_webhook_id()
    # Store secret but log redacted
    _webhooks[wh_id] = {
        "id": wh_id,
        "url": url,
        "secret": secret,  # stored encrypted in production
        "events": events,
        "created_by": _get_user_id(request),
        "active": True,
    }
    return {"webhook_id": wh_id, "status": "created"}


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook_delivery(request: Request, webhook_id: str):
    """Simulate a webhook delivery (for testing redaction in logs)."""
    _check_rbac(request, "webhook:update")
    wh = _webhooks.get(webhook_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found")

    # Simulate a delivery
    delivery_request = {
        "headers": {"Authorization": "Bearer sk-secret-key-12345", "Content-Type": "application/json"},
        "body": {"event": "test", "data": {"api_key": "sk-abc123def456"}},
    }
    delivery_response = {
        "status_code": 200,
        "headers": {"x-request-id": "req-98765"},
        "body": {"ok": True},
    }

    # Log with redaction
    webhook_delivery_logger.log_delivery_success(
        webhook_id=webhook_id,
        endpoint=wh["url"],
        request=delivery_request,
        response=delivery_response,
    )

    record = {
        "webhook_id": webhook_id,
        "endpoint": wh["url"],
        "status": "tested",
        "request": redact_webhook_payload(delivery_request),
    }
    _webhook_delivery_log.append(record)
    return record


@router.get("/webhooks/{webhook_id}/logs")
async def get_webhook_logs(request: Request, webhook_id: str):
    _check_rbac(request, "webhook:list")
    return {
        "webhook_id": webhook_id,
        "logs": [
            r for r in _webhook_delivery_log
            if r.get("webhook_id") == webhook_id
        ],
    }


@router.get("/webhooks")
async def list_webhooks(request: Request):
    _check_rbac(request, "webhook:list")
    return {"webhooks": [
        {k: v for k, v in wh.items() if k != "secret"}
        for wh in _webhooks.values()
    ]}


# ---- Storage / Manifest endpoints (Issue #841) ----------------------------

@router.post("/storage/manifest/register")
async def register_blob(request: Request, blob_id: str, blob_path: str):
    """Register a blob for integrity tracking."""
    _check_rbac(request, "agent:create")
    entry = manifest_checker.register_blob(blob_id, blob_path)
    return {
        "blob_id": blob_id,
        "digest": entry.expected_digest[:16] + "...",
        "size": entry.size_bytes,
    }


@router.get("/storage/manifest/verify/{blob_id}")
async def verify_blob(request: Request, blob_id: str):
    """Verify blob integrity. Returns alert if mismatch detected."""
    _check_rbac(request, "agent:list")
    status = manifest_checker.verify_blob(blob_id)
    result = {"blob_id": blob_id, "status": status.value}
    if status.value == "mismatch":
        alerts = manifest_checker.get_unacknowledged_alerts()
        result["alerts"] = [
            {"blob_id": a.blob_id, "expected": a.expected_digest[:16] + "...",
             "actual": a.actual_digest[:16] + "...", "timestamp": a.timestamp}
            for a in alerts if a.blob_id == blob_id
        ]
    return result


@router.get("/storage/alerts")
async def list_integrity_alerts(request: Request):
    """List all unacknowledged integrity alerts."""
    _check_rbac(request, "agent:list")
    alerts = manifest_checker.get_unacknowledged_alerts()
    return {
        "alerts": [
            {"blob_id": a.blob_id, "status": a.status.value, "timestamp": a.timestamp}
            for a in alerts
        ],
        "count": len(alerts),
    }


# ---- Legal Hold endpoints (Issue #828) ------------------------------------

@router.post("/storage/legal-hold")
async def place_legal_hold(request: Request, artifact_id: str, reason: str):
    _check_rbac(request, "rbac:modify")
    hold = legal_hold_manager.place_hold(
        artifact_id=artifact_id,
        reason=reason,
        placed_by=_get_user_id(request),
    )
    return {
        "artifact_id": artifact_id,
        "status": "hold_placed",
        "placed_at": hold.placed_at,
    }


@router.delete("/storage/legal-hold/{artifact_id}")
async def remove_legal_hold(request: Request, artifact_id: str):
    _check_rbac(request, "rbac:modify")
    if legal_hold_manager.remove_hold(artifact_id):
        return {"status": "hold_removed"}
    raise HTTPException(status_code=404, detail="No hold found")


@router.get("/storage/legal-hold")
async def list_legal_holds(request: Request):
    _check_rbac(request, "rbac:view")
    return {"holds": [
        {"artifact_id": h.artifact_id, "reason": h.reason,
         "placed_by": h.placed_by, "placed_at": h.placed_at}
        for h in legal_hold_manager.list_holds()
    ]}


# ---- RBAC management endpoints -------------------------------------------

@router.post("/rbac/role")
async def set_workspace_role(request: Request, workspace_id: str,
                              user_id: str, role: str):
    _check_rbac(request, "rbac:modify")
    workspace_role_manager.set_role(workspace_id, user_id, role)
    return {"workspace_id": workspace_id, "user_id": user_id, "role": role, "status": "set"}


@router.get("/rbac/role/{workspace_id}/{user_id}")
async def get_workspace_role(workspace_id: str, user_id: str):
    role = workspace_role_manager.get_role(workspace_id, user_id)
    if role is None:
        raise HTTPException(status_code=404, detail="User not found in workspace")
    return {"workspace_id": workspace_id, "user_id": user_id, "role": role}
