"""Bounded GitHub installation metadata reads with no durable credentials."""

from datetime import datetime, timedelta

import httpx
from pr_reliability_contracts.repositories import InstallationRepository, InstallationSnapshot


class InventoryError(RuntimeError):
    """Safe provider failure; callers must keep the previous inventory."""


async def fetch_inventory(installation_id, app_jwt, now, *, transport=None, timeout=10.0):
    try:
        async with httpx.AsyncClient(
            base_url="https://api.github.com/",
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pr-reliability-platform",
            },
        ) as client:
            response = await client.get(f"app/installations/{installation_id}")
            if response.status_code == 404:
                return InstallationSnapshot(installation_id=installation_id, state="deleted")
            response.raise_for_status()
            installation = response.json()
            if installation["id"] != installation_id or "suspended_at" not in installation:
                raise ValueError("invalid installation identity")
            if installation["suspended_at"] is not None:
                return InstallationSnapshot(installation_id=installation_id, state="suspended")
            response = await client.post(
                f"app/installations/{installation_id}/access_tokens",
                json={"permissions": {"metadata": "read"}},
            )
            if response.status_code != 201:
                raise ValueError("token request failed")
            credential = response.json()
            token = credential["token"]
            expiry = datetime.fromisoformat(credential["expires_at"])
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 16_384
                or not timedelta(seconds=30) <= expiry - now <= timedelta(minutes=65)
                or credential["permissions"] != {"metadata": "read"}
            ):
                raise ValueError("invalid inventory credential")
            client.headers["Authorization"] = f"Bearer {token}"
            repositories = []
            expected_count = None
            for page in range(1, 102):
                # Construct each URL ourselves; never follow untrusted pagination URLs.
                response = await client.get(
                    "installation/repositories", params={"per_page": 100, "page": page}
                )
                response.raise_for_status()
                payload = response.json()
                total = payload["total_count"]
                items = payload["repositories"]
                if (
                    type(total) is not int
                    or not 0 <= total <= 10_000
                    or not isinstance(items, list)
                    or len(items) > 100
                    or (expected_count is not None and total != expected_count)
                ):
                    raise ValueError("incomplete inventory")
                expected_count = total
                repositories.extend(InstallationRepository.model_validate(item) for item in items)
                if len(repositories) > total:
                    raise ValueError("inconsistent inventory")
                if len(items) < 100:
                    if len(repositories) != total:
                        raise ValueError("incomplete inventory")
                    return InstallationSnapshot(
                        installation_id=installation_id, state="active", repositories=repositories
                    )
            raise ValueError("inventory page limit exceeded")
    except Exception:  # noqa: BLE001 -- transport errors may contain credentials
        # Transport errors and response validation must not expose tokens or response bodies.
        raise InventoryError("GitHub inventory synchronization failed") from None
