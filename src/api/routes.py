"""API route definitions."""

from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, Depends
from typing import List, Dict, Optional

from src.agent import AgentRegistry, AgentStatus

router = APIRouter()
registry = AgentRegistry()

UNSAFE_URL_SCHEMES = {"javascript:", "data:", "file:", "vbscript:"}
PRIVATE_IP_PREFIXES = ("127.", "10.", "172.16.", "192.168.", "0.", "169.254.")


def is_safe_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme in UNSAFE_URL_SCHEMES:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if host == "localhost" or host == "127.0.0.1":
            return False
        if host.startswith(PRIVATE_IP_PREFIXES):
            return False
        return True
    except Exception:
        return False


@router.get("/agents")
async def list_agents(status: Optional[str] = None, group: Optional[str] = None):
    status_filter = AgentStatus(status) if status else None
    return {"agents": registry.list(status=status_filter, group=group)}


@router.post("/agents")
async def register_agent(name: str, agent_type: str, config: Optional[Dict] = None):
    if config:
        for key, value in config.items():
            if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://") or "callback" in key.lower() or "url" in key.lower() or "webhook" in key.lower()):
                if not is_safe_callback_url(value):
                    raise HTTPException(status_code=400, detail=f"Unsafe callback URL in config.{key}: {value}")
    agent_id = registry.register(name, agent_type, config)
    return {"agent_id": agent_id, "status": "registered"}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    if not registry.delete(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "deleted"}


@router.post("/agents/{agent_id}/start")
async def start_agent(agent_id: str):
    if not registry.update_status(agent_id, AgentStatus.RUNNING):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "started"}


@router.post("/agents/{agent_id}/stop")
async def stop_agent(agent_id: str):
    if not registry.update_status(agent_id, AgentStatus.PAUSED):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "stopped"}


@router.get("/agents/count")
async def agent_count():
    return {"count": registry.count()}
