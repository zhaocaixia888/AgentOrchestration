"""Agent Executor — Handles task execution within agent sandboxes with durable state."""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional
from uuid import uuid4


logger = logging.getLogger(__name__)

MAX_RESULT_PERSIST_RETRIES = 3


class AgentExecutor:
    def __init__(self, max_concurrent: int = 5, result_dir: Optional[str] = None):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, Any] = {}
        self._result_dir = result_dir or os.environ.get(
            "AO_RESULT_DIR",
            os.path.join(os.path.expanduser("~"), ".agentorch", "executor")
        )
        os.makedirs(self._result_dir, exist_ok=True)

    def _get_result_path(self, execution_id: str) -> str:
        safe_id = execution_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self._result_dir, f"{safe_id}.json")

    def _persist_result(self, execution_id: str, result: Any) -> None:
        """Persist execution result durably before any completion event emission."""
        result_path = self._get_result_path(execution_id)
        data = json.dumps({
            "execution_id": execution_id,
            "result": result,
            "timestamp": time.time(),
        }, default=str)
        for attempt in range(MAX_RESULT_PERSIST_RETRIES):
            try:
                tmp_path = result_path + ".tmp"
                with open(tmp_path, "w") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, result_path)
                return
            except OSError as e:
                logger.warning(
                    f"Result persist attempt {attempt + 1}/{MAX_RESULT_PERSIST_RETRIES} "
                    f"failed for execution {execution_id}: {e}"
                )
                if attempt == MAX_RESULT_PERSIST_RETRIES - 1:
                    raise

    def _load_persisted_result(self, execution_id: str) -> Optional[Any]:
        """Load result from durable storage for idempotent recovery."""
        result_path = self._get_result_path(execution_id)
        try:
            with open(result_path, "r") as f:
                data = json.load(f)
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    async def execute(self, agent_id: str, task: Dict[str, Any], handler: Callable) -> str:
        execution_id = str(uuid4())

        # Idempotency check: if this exact task was already completed, return cached
        # (This prevents duplicate work under retry scenarios)
        cached = self._load_persisted_result(execution_id)
        if cached is not None:
            logger.info(f"Execution {execution_id} already completed (idempotent replay)")
            self._results[execution_id] = cached
            return execution_id

        async with self._semaphore:
            task_obj = asyncio.create_task(
                self._run_execution(execution_id, agent_id, task, handler)
            )
            self._active_tasks[execution_id] = task_obj
            try:
                result = await task_obj
                # Persist durable state BEFORE storing in-memory (issue #760 fix)
                self._persist_result(execution_id, result)
                self._results[execution_id] = result
            except Exception as e:
                error_result = {"execution_id": execution_id, "error": str(e)}
                self._persist_result(execution_id, error_result)
                self._results[execution_id] = error_result
            finally:
                self._active_tasks.pop(execution_id, None)
        return execution_id

    async def _run_execution(self, exec_id: str, agent_id: str, task: Dict, handler: Callable) -> Any:
        start = time.time()
        result = await handler(agent_id, task)
        duration = time.time() - start
        return {
            "execution_id": exec_id,
            "agent_id": agent_id,
            "task_id": task.get("id"),
            "result": result,
            "duration": duration,
            "timestamp": time.time(),
        }

    def get_result(self, execution_id: str) -> Optional[Any]:
        # Fallback to persisted state if not in memory
        result = self._results.get(execution_id)
        if result is None:
            result = self._load_persisted_result(execution_id)
        return result

    def cancel(self, execution_id: str) -> bool:
        task = self._active_tasks.get(execution_id)
        if task and not task.done():
            task.cancel()
            # Persist cancelled state
            self._persist_result(execution_id, {"execution_id": execution_id, "status": "cancelled"})
            return True
        return False

    async def shutdown(self) -> None:
        for task in self._active_tasks.values():
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
