"""Temporal worker assembly and production entry points."""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import Callable

from pr_reliability_proof_adapter import ProofAdapter
from temporalio.client import Client
from temporalio.worker import Worker

from .activities import ActivityOperations, ReviewActivities
from .sandbox import DockerSandboxRunner
from .workflows import PullRequestReviewWorkflow


def create_workflow_worker(client: Client, task_queue: str) -> Worker:
    """Build the production workflow-task worker."""

    return Worker(
        client,
        task_queue=task_queue,
        workflows=[PullRequestReviewWorkflow],
    )


def create_activity_worker(
    client: Client,
    task_queue: str,
    activities: ReviewActivities,
) -> Worker:
    """Build one activity worker that registers the complete review activity set."""

    return Worker(
        client,
        task_queue=task_queue,
        activities=[
            activities.select_context,
            activities.analyze,
            activities.verify,
            activities.publish,
            activities.record_terminal,
        ],
    )


def create_worker(client: Client, task_queue: str, activities: ReviewActivities) -> Worker:
    """Build a combined workflow/activity worker for tests or compact deployments."""

    return Worker(
        client,
        task_queue=task_queue,
        workflows=[PullRequestReviewWorkflow],
        activities=[
            activities.select_context,
            activities.analyze,
            activities.verify,
            activities.publish,
            activities.record_terminal,
        ],
    )


async def run_workflow_worker_from_environment() -> None:
    """Poll production workflow tasks using environment configuration."""

    client, task_queue = await _connect_from_environment()
    await create_workflow_worker(client, task_queue).run()


async def run_activity_worker_from_environment() -> None:
    """Load provider operations and poll every review activity from one deployment."""

    operations = load_activity_operations(
        _required_environment("REVIEW_ACTIVITY_OPERATIONS_FACTORY")
    )
    client, task_queue = await _connect_from_environment()
    await create_activity_worker(client, task_queue, ReviewActivities(operations)).run()


def workflow_main() -> None:
    """Start the production workflow-task worker."""

    asyncio.run(run_workflow_worker_from_environment())


def activity_main() -> None:
    """Start the production provider-activity worker."""

    asyncio.run(run_activity_worker_from_environment())


def load_activity_operations(factory_path: str) -> ActivityOperations:
    """Load `module:factory` and require the complete activity-operation contract."""

    module_name, separator, attribute_name = factory_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("activity operations factory must use module:factory")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    if not isinstance(factory, Callable):
        raise TypeError("activity operations factory is not callable")
    operations = factory()
    if not isinstance(operations, ActivityOperations):
        raise TypeError("activity operations factory returned the wrong type")
    runner = operations.verify.runner
    if type(runner) is not DockerSandboxRunner or not runner.production_isolation_enabled:
        raise TypeError("production verification must use DockerSandboxRunner")
    proof = operations.verify.proof
    if type(proof) is not ProofAdapter or not proof.production_gate_enabled:
        raise TypeError("production verification must use PublishedProofGate")
    return operations


async def _connect_from_environment() -> tuple[Client, str]:
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "pr-review")
    client = await Client.connect(temporal_address, namespace=temporal_namespace)
    return client, task_queue


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value
