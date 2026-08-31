"""Owner-scoped dashboard pages and read APIs."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi import Path as ApiPath
from fastapi.responses import HTMLResponse, Response
from pr_reliability_contracts import (
    DashboardFinding,
    DashboardFindingStatus,
    DashboardOverview,
    DashboardRunDetail,
    DashboardRunPage,
    DashboardRunSummary,
    DashboardStage,
    DashboardStageName,
    DashboardStageStatus,
    DashboardTimelineEvent,
    Evidence,
    EvidenceKind,
    RunState,
    VerificationStatus,
)
from psycopg import Connection

from ..approvals import ApprovalInboxSettings
from ..reviewer import authorize_reviewer

ConnectionFactory = Callable[[], Connection[Any]]
_PACKAGED_WEB_ROOT = Path(__file__).parents[1] / "_web"
_SOURCE_WEB_ROOT = Path(__file__).parents[4] / "web"
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_EVENT_SUMMARIES = {
    "run.command_created": "Run queued from GitHub webhook",
    "run.cancelled": "Older run cancelled",
    "approval.decision_recorded": "Reviewer decision recorded",
    "approval.signal_created": "Approval delivery queued",
}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_dashboard_router(
    settings: ApprovalInboxSettings,
    connection_factory: ConnectionFactory,
) -> APIRouter:
    """Create static dashboard assets plus authenticated owner-scoped reads."""

    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_page() -> HTMLResponse:
        return HTMLResponse(
            _web_asset("dashboard.html").read_text(encoding="utf-8"),
            headers=_SECURITY_HEADERS,
        )

    @router.get("/dashboard/assets/dashboard.css", include_in_schema=False)
    def dashboard_styles() -> Response:
        return Response(
            _web_asset("dashboard.css").read_text(encoding="utf-8"),
            media_type="text/css",
            headers=_SECURITY_HEADERS,
        )

    @router.get("/dashboard/assets/dashboard.js", include_in_schema=False)
    def dashboard_script() -> Response:
        return Response(
            _web_asset("dashboard.js").read_text(encoding="utf-8"),
            media_type="text/javascript",
            headers=_SECURITY_HEADERS,
        )

    @router.get("/api/dashboard/overview", response_model=DashboardOverview)
    def overview(
        response: Response,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> DashboardOverview:
        authorize_reviewer(authorization, settings.reviewer_token)
        _protect_private_response(response)
        with connection_factory() as connection:
            row = connection.execute(
                """
                SELECT count(*)::integer,
                       count(*) FILTER (
                           WHERE state IN (
                               'queued', 'selecting_context', 'analyzing',
                               'verifying', 'awaiting_approval'
                           )
                       )::integer,
                       count(*) FILTER (WHERE state = 'awaiting_approval')::integer,
                       count(*) FILTER (WHERE state = 'failed')::integer,
                       count(*) FILTER (WHERE state = 'published')::integer,
                       percentile_cont(0.50) WITHIN GROUP (
                           ORDER BY extract(epoch FROM (updated_at - created_at)) * 1000
                       ) FILTER (
                           WHERE state IN ('published', 'rejected', 'failed', 'cancelled')
                       ) AS p50_duration_ms,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY extract(epoch FROM (updated_at - created_at)) * 1000
                       ) FILTER (
                           WHERE state IN ('published', 'rejected', 'failed', 'cancelled')
                       ) AS p95_duration_ms
                FROM runs
                WHERE owner_id = %s
                """,
                (settings.owner_id,),
            ).fetchone()
            pending_findings = connection.execute(
                """
                SELECT count(*)::integer
                FROM findings AS finding
                JOIN runs AS run
                  ON run.id = finding.run_id
                 AND run.owner_id = finding.owner_id
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                LEFT JOIN approvals AS approval
                  ON approval.finding_id = finding.id
                 AND approval.owner_id = finding.owner_id
                WHERE finding.owner_id = %s
                  AND run.state = 'awaiting_approval'
                  AND run.head_sha = pull_request.head_sha
                  AND approval.id IS NULL
                """,
                (settings.owner_id,),
            ).fetchone()[0]
        total_runs, active, awaiting, failed, published, p50, p95 = row
        return DashboardOverview(
            schema_version="1",
            total_runs=total_runs,
            active_runs=active,
            awaiting_approval_runs=awaiting,
            pending_findings=pending_findings,
            failed_runs=failed,
            published_runs=published,
            p50_duration_ms=_optional_int(p50),
            p95_duration_ms=_optional_int(p95),
            activity_retry_count=None,
            usage_complete_runs=0,
            usage_partial_runs=0,
            usage_unknown_runs=total_runs,
            exact_known_cost_usd_micros=None,
        )

    @router.get("/api/dashboard/runs", response_model=DashboardRunPage)
    def list_runs(
        response: Response,
        authorization: str | None = Header(default=None, alias="Authorization"),
        run_status: Annotated[RunState | None, Query(alias="status")] = None,
        repository: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> DashboardRunPage:
        authorize_reviewer(authorization, settings.reviewer_token)
        _protect_private_response(response)
        state_value = run_status.value if run_status is not None else None
        with connection_factory() as connection:
            total = connection.execute(
                """
                SELECT count(*)::integer
                FROM runs AS run
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                JOIN repositories AS repository
                  ON repository.id = pull_request.repository_id
                 AND repository.owner_id = run.owner_id
                WHERE run.owner_id = %s
                  AND (%s::text IS NULL OR run.state = %s)
                  AND (%s::text IS NULL OR repository.full_name = %s)
                """,
                (settings.owner_id, state_value, state_value, repository, repository),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT run.public_id, repository.full_name, pull_request.github_number,
                       run.head_sha, run.generation, run.state,
                       count(DISTINCT finding.id)::integer,
                       count(DISTINCT finding.id) FILTER (
                           WHERE approval.id IS NULL
                             AND run.state = 'awaiting_approval'
                             AND run.head_sha = pull_request.head_sha
                       )::integer,
                       greatest(
                           0,
                           round(extract(epoch FROM (
                               CASE WHEN run.state IN (
                                        'published', 'rejected', 'failed', 'cancelled'
                                    )
                                    THEN run.updated_at ELSE now() END - run.created_at
                           )) * 1000)
                       )::bigint,
                       run.created_at, run.updated_at
                FROM runs AS run
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                JOIN repositories AS repository
                  ON repository.id = pull_request.repository_id
                 AND repository.owner_id = run.owner_id
                LEFT JOIN findings AS finding
                  ON finding.run_id = run.id
                 AND finding.owner_id = run.owner_id
                LEFT JOIN approvals AS approval
                  ON approval.finding_id = finding.id
                 AND approval.owner_id = finding.owner_id
                WHERE run.owner_id = %s
                  AND (%s::text IS NULL OR run.state = %s)
                  AND (%s::text IS NULL OR repository.full_name = %s)
                GROUP BY run.id, repository.full_name, pull_request.github_number
                ORDER BY run.created_at DESC, run.id DESC
                LIMIT %s OFFSET %s
                """,
                (
                    settings.owner_id,
                    state_value,
                    state_value,
                    repository,
                    repository,
                    limit,
                    offset,
                ),
            ).fetchall()
        return DashboardRunPage(
            schema_version="1",
            items=tuple(_run_summary(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/api/dashboard/runs/{run_id}", response_model=DashboardRunDetail)
    def run_detail(
        run_id: Annotated[
            str,
            ApiPath(
                min_length=26,
                max_length=26,
                pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$",
            ),
        ],
        response: Response,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> DashboardRunDetail:
        authorize_reviewer(authorization, settings.reviewer_token)
        _protect_private_response(response)
        with connection_factory() as connection:
            row = connection.execute(
                """
                SELECT run.public_id, repository.full_name, pull_request.github_number,
                       run.head_sha, run.generation, run.state,
                       count(DISTINCT finding.id)::integer,
                       count(DISTINCT finding.id) FILTER (
                           WHERE approval.id IS NULL
                             AND run.state = 'awaiting_approval'
                             AND run.head_sha = pull_request.head_sha
                       )::integer,
                       greatest(
                           0,
                           round(extract(epoch FROM (
                               CASE WHEN run.state IN (
                                        'published', 'rejected', 'failed', 'cancelled'
                                    )
                                    THEN run.updated_at ELSE now() END - run.created_at
                           )) * 1000)
                       )::bigint,
                       run.created_at, run.updated_at, run.id
                FROM runs AS run
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                JOIN repositories AS repository
                  ON repository.id = pull_request.repository_id
                 AND repository.owner_id = run.owner_id
                LEFT JOIN findings AS finding
                  ON finding.run_id = run.id
                 AND finding.owner_id = run.owner_id
                LEFT JOIN approvals AS approval
                  ON approval.finding_id = finding.id
                 AND approval.owner_id = finding.owner_id
                WHERE run.owner_id = %s AND run.public_id = %s
                GROUP BY run.id, repository.full_name, pull_request.github_number
                """,
                (settings.owner_id, run_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
            internal_run_id = row[-1]
            event_rows = connection.execute(
                """
                SELECT event_type, occurred_at, event_data->>'traceparent',
                       event_data->>'status', event_data->>'reason'
                FROM run_events
                WHERE owner_id = %s AND run_id = %s
                ORDER BY occurred_at, id
                """,
                (settings.owner_id, internal_run_id),
            ).fetchall()
            finding_rows = connection.execute(
                """
                SELECT finding.public_id, finding.category, finding.severity,
                       finding.claim, finding.confidence, finding.evidence,
                       CASE
                           WHEN approval.decision IS NOT NULL THEN approval.decision
                           WHEN run.state = 'awaiting_approval'
                            AND run.head_sha = pull_request.head_sha THEN 'pending'
                           ELSE 'not_actionable'
                       END
                FROM findings AS finding
                JOIN runs AS run
                  ON run.id = finding.run_id
                 AND run.owner_id = finding.owner_id
                JOIN pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                 AND pull_request.owner_id = run.owner_id
                LEFT JOIN approvals AS approval
                  ON approval.finding_id = finding.id
                 AND approval.owner_id = finding.owner_id
                WHERE finding.owner_id = %s AND finding.run_id = %s
                ORDER BY finding.id
                """,
                (settings.owner_id, internal_run_id),
            ).fetchall()
        trace_id = next(
            (
                match.group(1)
                for _, _, traceparent, _, _ in event_rows
                if traceparent and (match := _TRACEPARENT.fullmatch(traceparent))
            ),
            None,
        )
        return DashboardRunDetail(
            schema_version="1",
            run=_run_summary(row[:-1]),
            trace_id=trace_id,
            stages=_stage_progress(row[5], event_rows),
            events=tuple(
                DashboardTimelineEvent(
                    schema_version="1",
                    event_type=event_type,
                    summary=_event_summary(event_type, event_status, reason),
                    occurred_at=occurred_at,
                )
                for event_type, occurred_at, _, event_status, reason in event_rows
            ),
            findings=tuple(_finding(item) for item in finding_rows),
        )

    return router


def _web_asset(name: str) -> Path:
    packaged = _PACKAGED_WEB_ROOT / name
    return packaged if packaged.exists() else _SOURCE_WEB_ROOT / name


def _optional_int(value: object) -> int | None:
    return None if value is None else max(0, round(float(value)))


def _run_summary(row: tuple[Any, ...]) -> DashboardRunSummary:
    return DashboardRunSummary(
        schema_version="1",
        run_id=row[0],
        repository_full_name=row[1],
        pull_request_number=row[2],
        head_sha=row[3],
        generation=row[4],
        state=row[5],
        finding_count=row[6],
        pending_finding_count=row[7],
        retry_count=None,
        duration_ms=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def _finding(row: tuple[Any, ...]) -> DashboardFinding:
    evidence = tuple(Evidence.model_validate(item) for item in row[5])
    test_results = [item for item in evidence if item.kind is EvidenceKind.TEST_RESULT]
    if not test_results:
        verification = VerificationStatus.NOT_RECORDED
    elif any(item.exit_code != 0 for item in test_results):
        verification = VerificationStatus.FAILED
    else:
        verification = VerificationStatus.PASSED
    return DashboardFinding(
        schema_version="1",
        finding_id=row[0],
        category=row[1],
        severity=row[2],
        claim=row[3],
        confidence=row[4],
        evidence=evidence,
        verification=verification,
        approval_status=DashboardFindingStatus(row[6]),
    )


def _protect_private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _event_summary(event_type: str, event_status: str | None, reason: str | None) -> str:
    if event_type == "run.command_dispatched":
        if event_status == "accepted":
            return "Run accepted by workflow"
        if event_status == "skipped" and reason == "superseded generation":
            return "Superseded run dispatch skipped"
        if event_status == "skipped":
            return "Run dispatch skipped"
    if event_type == "approval.signal_dispatched":
        if event_status == "accepted":
            return "Approval delivered to workflow"
        if event_status == "skipped":
            return "Approval delivery skipped for inactive commit"
    return _EVENT_SUMMARIES.get(event_type, "Run event recorded")


def _stage_progress(
    run_state: str,
    event_rows: list[tuple[Any, ...]],
) -> tuple[DashboardStage, ...]:
    names = tuple(DashboardStageName)
    statuses = {name: DashboardStageStatus.NOT_STARTED for name in names}
    statuses[DashboardStageName.WEBHOOK] = DashboardStageStatus.COMPLETED

    dispatch_receipts = [
        event_status
        for event_type, _, _, event_status, _ in event_rows
        if event_type == "run.command_dispatched"
    ]
    if "accepted" in dispatch_receipts:
        statuses[DashboardStageName.DISPATCH] = DashboardStageStatus.COMPLETED
    elif "skipped" in dispatch_receipts:
        statuses[DashboardStageName.DISPATCH] = DashboardStageStatus.SKIPPED
    else:
        statuses[DashboardStageName.DISPATCH] = DashboardStageStatus.CURRENT

    state = RunState(run_state)
    progress = {
        RunState.SELECTING_CONTEXT: (DashboardStageName.SELECT_CONTEXT, ()),
        RunState.ANALYZING: (
            DashboardStageName.ANALYZE,
            (DashboardStageName.SELECT_CONTEXT,),
        ),
        RunState.VERIFYING: (
            DashboardStageName.VERIFY,
            (DashboardStageName.SELECT_CONTEXT, DashboardStageName.ANALYZE),
        ),
        RunState.AWAITING_APPROVAL: (
            DashboardStageName.APPROVAL,
            (
                DashboardStageName.SELECT_CONTEXT,
                DashboardStageName.ANALYZE,
                DashboardStageName.VERIFY,
            ),
        ),
    }
    if state in progress:
        current, completed = progress[state]
        statuses[DashboardStageName.DISPATCH] = DashboardStageStatus.COMPLETED
        statuses[current] = DashboardStageStatus.CURRENT
        for name in completed:
            statuses[name] = DashboardStageStatus.COMPLETED
    elif state is RunState.PUBLISHED:
        statuses = {name: DashboardStageStatus.COMPLETED for name in names}
    elif state is RunState.REJECTED:
        for name in names[:-1]:
            statuses[name] = DashboardStageStatus.COMPLETED
        statuses[DashboardStageName.PUBLISH] = DashboardStageStatus.SKIPPED
    elif state in {RunState.FAILED, RunState.CANCELLED}:
        for name in names[2:]:
            statuses[name] = DashboardStageStatus.UNKNOWN

    return tuple(
        DashboardStage(schema_version="1", name=name, status=statuses[name]) for name in names
    )
