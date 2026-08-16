"""Authenticated approval inbox API."""

from .inbox import ApprovalInboxSettings, create_approval_inbox_router

__all__ = ["ApprovalInboxSettings", "create_approval_inbox_router"]
