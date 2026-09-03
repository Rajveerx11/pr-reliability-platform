"""Production provider operations."""

from .checkout import ExactHeadCheckout, GitHubCheckoutError, RepositoryCheckout
from .github_app import (
    CHECKOUT_PERMISSIONS,
    REVIEW_PERMISSIONS,
    GitHubAppAuthenticationError,
    GitHubAppInstallationTokenProvider,
    GitHubInstallationToken,
)
from .openai import OpenAIResponsesClient


def create_operations():
    """Load production assembly only when worker startup requests it."""

    from .factory import create_operations as build_operations

    return build_operations()


__all__ = [
    "CHECKOUT_PERMISSIONS",
    "REVIEW_PERMISSIONS",
    "ExactHeadCheckout",
    "GitHubAppAuthenticationError",
    "GitHubAppInstallationTokenProvider",
    "GitHubCheckoutError",
    "GitHubInstallationToken",
    "OpenAIResponsesClient",
    "RepositoryCheckout",
    "create_operations",
]
