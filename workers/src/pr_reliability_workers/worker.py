"""Temporal worker assembly."""

from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import ReviewActivities
from .workflows import PullRequestReviewWorkflow


def create_worker(client: Client, task_queue: str, activities: ReviewActivities) -> Worker:
    """Build a worker with every workflow-owned activity registered."""

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
