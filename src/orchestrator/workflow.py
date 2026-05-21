"""Workflow Manager — Defines and executes multi-step agent workflows.

Supports duplicate node rejection (#943), partial rollback protection (#955),
and compensating actions for failed workflows.
"""

import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4


logger = logging.getLogger(__name__)


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class WorkflowStep:
    def __init__(self, name: str, handler: Callable, retries: int = 0, timeout: int = 300,
                 compensator: Optional[Callable] = None):
        self.id = str(uuid4())
        self.name = name
        self.handler = handler
        self.retries = retries
        self.timeout = timeout
        self.status = StepStatus.PENDING
        self.result: Any = None
        self.error: Optional[str] = None
        self.compensator = compensator  # Optional rollback handler

    def compensate(self) -> None:
        """Execute compensation action for rollback."""
        if self.compensator:
            try:
                self.compensator(self.result)
                self.status = StepStatus.ROLLED_BACK
                logger.info(f"Step {self.name} compensated (rolled back)")
            except Exception as e:
                logger.error(f"Compensation failed for step {self.name}: {e}")
                self.status = StepStatus.ROLLED_BACK  # Mark as rolled back regardless


class Workflow:
    def __init__(self, name: str, description: str = ""):
        self.id = str(uuid4())
        self.name = name
        self.description = description
        self.steps: List[WorkflowStep] = []
        self._step_map: Dict[str, WorkflowStep] = {}
        self._step_name_map: Dict[str, str] = {}  # name -> step_id
        self.status = StepStatus.PENDING

    def add_step(self, step: WorkflowStep) -> "Workflow":
        """Add a step, rejecting duplicate node identifiers (#943)."""
        # Reject duplicate step names (node identifiers must be unique)
        if step.name in self._step_name_map:
            existing_id = self._step_name_map[step.name]
            raise ValueError(
                f"Duplicate node identifier '{step.name}': "
                f"a step with this name already exists (id={existing_id}). "
                f"Workflow node identifiers must be unique."
            )

        self.steps.append(step)
        self._step_map[step.id] = step
        self._step_name_map[step.name] = step.id
        return self

    def get_step(self, step_id: str) -> Optional[WorkflowStep]:
        return self._step_map.get(step_id)

    def get_step_by_name(self, name: str) -> Optional[WorkflowStep]:
        """Look up a step by its unique name."""
        step_id = self._step_name_map.get(name)
        if step_id:
            return self._step_map.get(step_id)
        return None


class WorkflowManager:
    def __init__(self):
        self._workflows: Dict[str, Workflow] = {}

    def create_workflow(self, name: str, description: str = "") -> Workflow:
        workflow = Workflow(name, description)
        self._workflows[workflow.id] = workflow
        return workflow

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        return self._workflows.get(workflow_id)

    def list_workflows(self) -> List[Workflow]:
        return list(self._workflows.values())

    def delete_workflow(self, workflow_id: str) -> bool:
        return self._workflows.pop(workflow_id, None) is not None

    def execute_workflow(self, workflow_id: str) -> bool:
        """Execute workflow steps, blocking downstream after partial rollback (#955)."""
        workflow = self._workflows.get(workflow_id)
        if not workflow:
            return False

        workflow.status = StepStatus.RUNNING
        completed_steps: List[WorkflowStep] = []
        has_failure = False

        for step in workflow.steps:
            # Block downstream if a prior step has failed or been rolled back (#955)
            if has_failure:
                step.status = StepStatus.SKIPPED
                logger.info(
                    f"Step '{step.name}' blocked (downstream) — "
                    f"prior step failure triggered partial rollback protection"
                )
                continue

            step.status = StepStatus.RUNNING
            try:
                result = step.handler()
                step.result = result
                step.status = StepStatus.COMPLETED
                completed_steps.append(step)
            except Exception as e:
                step.error = str(e)
                step.status = StepStatus.FAILED
                workflow.status = StepStatus.FAILED
                has_failure = True

                # Execute compensation (rollback) for already-completed steps (#955)
                for completed in reversed(completed_steps):
                    completed.compensate()

                logger.warning(
                    f"Workflow '{workflow.name}' failed at step '{step.name}': {e}. "
                    f"Rolled back {len(completed_steps)} completed step(s). "
                    f"Downstream steps blocked."
                )
                return False

        if not has_failure:
            workflow.status = StepStatus.COMPLETED
            logger.info(f"Workflow '{workflow.name}' completed successfully")
        return True
