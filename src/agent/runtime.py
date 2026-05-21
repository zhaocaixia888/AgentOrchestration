"""Agent Runtime — Manages agent process lifecycle with durable state persistence.

State machine states: STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED
Crashed transitions: any state -> CRASHED
Durable persistence: state is persisted before any completion event emission.
"""

import json
import os
import signal
import subprocess
import logging
import time
from enum import Enum
from typing import Dict, Optional


logger = logging.getLogger(__name__)


class RuntimeState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


# Valid state transitions: from_state -> {to_state, ...}
VALID_TRANSITIONS: Dict[RuntimeState, set] = {
    RuntimeState.STOPPED: {RuntimeState.STARTING},
    RuntimeState.STARTING: {RuntimeState.RUNNING, RuntimeState.CRASHED, RuntimeState.STOPPED},
    RuntimeState.RUNNING: {RuntimeState.STOPPING, RuntimeState.CRASHED},
    RuntimeState.STOPPING: {RuntimeState.STOPPED, RuntimeState.CRASHED},
    RuntimeState.CRASHED: {RuntimeState.STARTING},  # restart allowed
}

MAX_STATE_PERSIST_RETRIES = 3


class StateTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


class AgentRuntime:
    def __init__(self, state_dir: Optional[str] = None):
        self._processes: Dict[str, subprocess.Popen] = {}
        self._states: Dict[str, RuntimeState] = {}
        self._state_dir = state_dir or os.environ.get(
            "AO_STATE_DIR",
            os.path.join(os.path.expanduser("~"), ".agentorch", "runtime")
        )
        os.makedirs(self._state_dir, exist_ok=True)

    def _get_state_path(self, agent_id: str) -> str:
        safe_id = agent_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self._state_dir, f"{safe_id}.json")

    def _persist_state(self, agent_id: str, state: RuntimeState) -> None:
        """Persist agent state durably with bounded retries."""
        state_path = self._get_state_path(agent_id)
        data = json.dumps({
            "agent_id": agent_id,
            "state": state.value,
            "timestamp": time.time(),
        })
        for attempt in range(MAX_STATE_PERSIST_RETRIES):
            try:
                # Atomic write: write to temp file then rename
                tmp_path = state_path + ".tmp"
                with open(tmp_path, "w") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, state_path)
                return
            except OSError as e:
                logger.warning(
                    f"State persist attempt {attempt + 1}/{MAX_STATE_PERSIST_RETRIES} "
                    f"failed for agent {agent_id}: {e}"
                )
                if attempt == MAX_STATE_PERSIST_RETRIES - 1:
                    raise

    def _load_persisted_state(self, agent_id: str) -> Optional[RuntimeState]:
        """Recover state from durable storage (for idempotent recovery)."""
        state_path = self._get_state_path(agent_id)
        try:
            with open(state_path, "r") as f:
                data = json.load(f)
            return RuntimeState(data["state"])
        except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
            return None

    def _validate_transition(self, agent_id: str, from_state: RuntimeState, to_state: RuntimeState) -> None:
        """Validate state machine transition guard."""
        allowed = VALID_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise StateTransitionError(
                f"Invalid state transition for agent {agent_id}: "
                f"{from_state.value} -> {to_state.value}. "
                f"Allowed from {from_state.value}: {[s.value for s in allowed]}"
            )

    def _transition_to(self, agent_id: str, to_state: RuntimeState) -> RuntimeState:
        """Perform a guarded, persisted state transition. Returns the new state."""
        from_state = self._states.get(agent_id, RuntimeState.STOPPED)
        self._validate_transition(agent_id, from_state, to_state)

        # Persist BEFORE updating in-memory state (durable guard)
        self._persist_state(agent_id, to_state)
        self._states[agent_id] = to_state
        logger.info(f"Agent {agent_id}: {from_state.value} -> {to_state.value}")
        return to_state

    def start(self, agent_id: str, command: list, env: Optional[Dict] = None) -> bool:
        # Check if already running
        if agent_id in self._processes and self._processes[agent_id].poll() is None:
            logger.warning(f"Agent {agent_id} is already running")
            return False

        # Recover from persisted state for idempotent retry
        persisted = self._load_persisted_state(agent_id)
        if persisted == RuntimeState.RUNNING:
            # If we persisted RUNNING but process is dead, it crashed
            self._states[agent_id] = RuntimeState.CRASHED
            self._persist_state(agent_id, RuntimeState.CRASHED)

        # Guard: STARTING transition
        self._transition_to(agent_id, RuntimeState.STARTING)

        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        process_env["AO_AGENT_ID"] = agent_id

        try:
            proc = subprocess.Popen(
                command,
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._processes[agent_id] = proc
            self._transition_to(agent_id, RuntimeState.RUNNING)
            logger.info(f"Agent {agent_id} started (PID: {proc.pid})")
            return True
        except Exception as e:
            self._transition_to(agent_id, RuntimeState.CRASHED)
            logger.error(f"Failed to start agent {agent_id}: {e}")
            return False

    def complete(self, agent_id: str) -> bool:
        """Persist final completed state before emitting completion.
        
        This is the key fix for issue #760: state is persisted durably
        BEFORE any completion event or callback is emitted.
        """
        current = self._states.get(agent_id, RuntimeState.STOPPED)
        if current == RuntimeState.RUNNING:
            self._transition_to(agent_id, RuntimeState.STOPPING)
            self._transition_to(agent_id, RuntimeState.STOPPED)
            return True
        elif current == RuntimeState.STOPPING:
            self._transition_to(agent_id, RuntimeState.STOPPED)
            return True
        return False

    def stop(self, agent_id: str, timeout: int = 10) -> bool:
        proc = self._processes.get(agent_id)
        if not proc or proc.poll() is not None:
            return False

        self._transition_to(agent_id, RuntimeState.STOPPING)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        self._transition_to(agent_id, RuntimeState.STOPPED)
        logger.info(f"Agent {agent_id} stopped")
        return True

    def get_state(self, agent_id: str) -> RuntimeState:
        proc = self._processes.get(agent_id)
        if proc and proc.poll() is not None:
            try:
                self._transition_to(agent_id, RuntimeState.CRASHED)
            except StateTransitionError:
                pass
        return self._states.get(agent_id, RuntimeState.STOPPED)

    def is_running(self, agent_id: str) -> bool:
        proc = self._processes.get(agent_id)
        return proc is not None and proc.poll() is None
