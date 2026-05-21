"""Orchestration Engine — Core execution and coordination logic.

Preserves parent failure state when late child events arrive (#854).
Rejects state transitions from FAILED -> RUNNING when processing child success.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from src.agent import AgentRegistry, AgentStatus
from src.orchestrator.scheduler import TaskScheduler

logger = logging.getLogger(__name__)


class EngineState(Enum):
    """Engine-level state for parent workflows."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"


class ParentStateReducer:
    """Guards parent workflow state transitions, preserving failure state (#854).
    
    Late child events (success events arriving after parent has failed) are
    rejected to preserve the parent's FAILED state.
    """

    # Valid transitions: FAILED state is a terminal state for late child events
    _VALID_TRANSITIONS: Dict[EngineState, set] = {
        EngineState.PENDING: {EngineState.RUNNING},
        EngineState.RUNNING: {EngineState.COMPLETED, EngineState.FAILED, EngineState.ROLLING_BACK},
        EngineState.COMPLETED: set(),  # terminal
        EngineState.FAILED: {EngineState.ROLLING_BACK},  # terminal for child success (#854)
        EngineState.ROLLING_BACK: {EngineState.FAILED, EngineState.COMPLETED},
    }

    def __init__(self):
        self._parent_states: Dict[str, EngineState] = {}
        self._parent_attempts: Dict[str, int] = {}
        self._parent_revisions: Dict[str, int] = {}

    def initialize(self, parent_id: str, attempt: int = 1, revision: int = 1) -> EngineState:
        """Initialize parent state with attempt and revision tracking."""
        self._parent_states[parent_id] = EngineState.PENDING
        self._parent_attempts[parent_id] = attempt
        self._parent_revisions[parent_id] = revision
        return EngineState.PENDING

    def get_state(self, parent_id: str) -> EngineState:
        return self._parent_states.get(parent_id, EngineState.PENDING)

    def transition(self, parent_id: str, target: EngineState,
                   attempt: Optional[int] = None,
                   revision: Optional[int] = None) -> bool:
        """Attempt a guarded state transition. Returns True on success, False on rejection."""
        current = self._parent_states.get(parent_id, EngineState.PENDING)

        # Stale attempt/revision check: reject out-of-order transitions
        if attempt is not None:
            stored_attempt = self._parent_attempts.get(parent_id, 0)
            if attempt < stored_attempt:
                logger.warning(
                    f"Parent {parent_id}: rejecting state transition {current.value} -> {target.value} "
                    f"— stale attempt {attempt} < current {stored_attempt}"
                )
                return False

        if revision is not None:
            stored_revision = self._parent_revisions.get(parent_id, 0)
            if revision < stored_revision:
                logger.warning(
                    f"Parent {parent_id}: rejecting state transition {current.value} -> {target.value} "
                    f"— stale revision {revision} < current {stored_revision}"
                )
                return False

        allowed = self._VALID_TRANSITIONS.get(current, set())

        # Issue #854: FAILED -> RUNNING is explicitly rejected
        # When a parent has failed and a late child success event arrives,
        # the child's event tries to update parent to RUNNING/COMPLETED.
        # This guard preserves the parent's FAILED state.
        if current == EngineState.FAILED and target in (EngineState.RUNNING, EngineState.COMPLETED):
            logger.warning(
                f"Parent {parent_id}: rejecting late child event transition "
                f"{current.value} -> {target.value} — parent already FAILED (#854)"
            )
            return False

        if target not in allowed:
            logger.warning(
                f"Parent {parent_id}: invalid transition {current.value} -> {target.value}"
            )
            return False

        # Execute transition
        self._parent_states[parent_id] = target
        if attempt is not None:
            self._parent_attempts[parent_id] = attempt
        if revision is not None:
            self._parent_revisions[parent_id] = revision

        logger.info(f"Parent {parent_id}: {current.value} -> {target.value}")
        return True

    def record_child_event(self, parent_id: str, child_id: str, child_status: str) -> bool:
        """Record a child event, guarding against late arrivals (#854)."""
        parent_state = self._parent_states.get(parent_id, EngineState.PENDING)

        # Late child success event when parent has already failed: reject
        if parent_state == EngineState.FAILED and child_status == "completed":
            logger.warning(
                f"Parent {parent_id}: discarding late child success event from "
                f"child {child_id} — parent already FAILED (issue #854)"
            )
            return False

        # Late child event when parent has already completed: still track but don't transition
        if parent_state == EngineState.COMPLETED:
            logger.info(
                f"Parent {parent_id}: late child event from {child_id} "
                f"tracked but parent already COMPLETED"
            )
            return True

        return True


class OrchestrationEngine:
    def __init__(self, max_workers: int = 10, agent_timeout: int = 300):
        self.registry = AgentRegistry()
        self.scheduler = TaskScheduler()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.agent_timeout = agent_timeout
        self._running = False
        self._hooks: Dict[str, List[Callable]] = {
            "pre_execute": [],
            "post_execute": [],
            "on_error": [],
            "on_complete": [],
        }
        self._parent_reducer = ParentStateReducer()
        self._child_events: Dict[str, List[Dict]] = {}  # parent_id -> child events

    def initialize_parent(self, parent_id: str, attempt: int = 1, revision: int = 1) -> None:
        """Initialize a parent workflow with state tracking."""
        self._parent_reducer.initialize(parent_id, attempt, revision)
        self._child_events[parent_id] = []
        logger.info(f"Parent {parent_id} initialized (attempt={attempt}, revision={revision})")

    def get_parent_state(self, parent_id: str) -> EngineState:
        return self._parent_reducer.get_state(parent_id)

    def register_hook(self, event: str, callback: Callable) -> None:
        if event in self._hooks:
            self._hooks[event].append(callback)

    async def start(self) -> None:
        self._running = True
        logger.info("Orchestration engine started")
        while self._running:
            task = await self.scheduler.dequeue()
            if task:
                asyncio.create_task(self._execute_task(task))
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        logger.info("Orchestration engine stopped")

    async def handle_child_event(self, task_id: str, child_id: str, child_status: str,
                                  child_result: Any = None) -> None:
        """Handle a child event, preserving parent failure on late arrivals (#854)."""
        # Find the parent workflow for this task
        parent_id = None
        for pid, events in self._child_events.items():
            if any(e.get("task_id") == task_id for e in events):
                parent_id = pid
                break

        if parent_id is None:
            logger.warning(f"No parent found for task {task_id} child event")
            return

        event = {
            "task_id": task_id,
            "child_id": child_id,
            "status": child_status,
            "result": child_result,
            "timestamp": asyncio.get_event_loop().time(),
        }
        self._child_events[parent_id].append(event)

        # The parent state reducer will reject late child success events
        # when parent has already failed (issue #854)
        accepted = self._parent_reducer.record_child_event(parent_id, child_id, child_status)

        if accepted and child_status == "completed":
            # Only transition parent if there's an open child that succeeded
            # and parent hasn't already transitioned
            all_children_done = all(
                e["status"] in ("completed", "failed")
                for e in self._child_events[parent_id]
            )
            if all_children_done:
                current = self._parent_reducer.get_state(parent_id)
                if current == EngineState.RUNNING:
                    self._parent_reducer.transition(parent_id, EngineState.COMPLETED)

    async def _execute_task(self, task: Dict[str, Any]) -> None:
        task_id = task["id"]
        agent_id = task["target_agent"]
        parent_id = task.get("parent_id")
        parent_attempt = task.get("parent_attempt", 1)
        parent_revision = task.get("parent_revision", 1)

        logger.info(f"Executing task {task_id} on agent {agent_id}")

        for hook in self._hooks["pre_execute"]:
            await hook(task)

        try:
            agent = self.registry.get(agent_id)
            if not agent:
                raise ValueError(f"Agent {agent_id} not found")

            # Initialize parent if this task has a parent context
            if parent_id and parent_id not in self._child_events:
                self.initialize_parent(parent_id, parent_attempt, parent_revision)
                if parent_id:
                    self._parent_reducer.transition(
                        parent_id, EngineState.RUNNING,
                        attempt=parent_attempt, revision=parent_revision
                    )

            self.registry.update_status(agent_id, AgentStatus.RUNNING)
            result = await asyncio.wait_for(
                self._run_agent_task(agent, task),
                timeout=self.agent_timeout,
            )
            self.registry.update_status(agent_id, AgentStatus.PAUSED)

            for hook in self._hooks["post_execute"]:
                await hook(task, result)

            logger.info(f"Task {task_id} completed successfully")

            # Handle child completion event (parent guard applied)
            if parent_id:
                await self.handle_child_event(task_id, task_id, "completed", result)

        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")

            # Transition parent to FAILED if applicable
            if parent_id:
                self._parent_reducer.transition(
                    parent_id, EngineState.FAILED,
                    attempt=parent_attempt, revision=parent_revision
                )

            for hook in self._hooks["on_error"]:
                await hook(task, e)

    async def _run_agent_task(self, agent: Dict, task: Dict) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._execute_in_thread,
            agent,
            task,
        )

    def _execute_in_thread(self, agent: Dict, task: Dict) -> Any:
        return {"status": "completed", "output": f"Task {task['id']} processed by {agent['name']}"}
