"""Shared OpenTelemetry configuration and safe trace propagation."""

from __future__ import annotations

import os
from typing import Any

from opentelemetry import metrics, propagate, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer
from temporalio.contrib.opentelemetry import (
    TracingInterceptor,
    TracingWorkflowInboundInterceptor,
)
from temporalio.worker import ExecuteWorkflowInput, WorkflowInterceptorClassInput

_INSTRUMENTATION_NAME = "pr-reliability-platform"


class PersistedTraceWorkflowInboundInterceptor(TracingWorkflowInboundInterceptor):
    """Use a workflow generation's saved trace parent for its outbound work."""

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        traceparent = getattr(input.args[0], "traceparent", None) if input.args else None
        if isinstance(traceparent, str):
            # Persist only W3C trace identity. The command contract validates its shape.
            self._workflow_context_carrier = {"traceparent": traceparent}
        return await super().execute_workflow(input)


class PersistedTraceTracingInterceptor(TracingInterceptor):
    """Temporal tracing that resets context at each continue-as-new generation."""

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[TracingWorkflowInboundInterceptor]:
        super().workflow_interceptor_class(input)
        return PersistedTraceWorkflowInboundInterceptor


def configure_telemetry(service_name: str) -> TracingInterceptor:
    """Configure one process and return Temporal trace propagation."""

    resource = Resource.create({SERVICE_NAME: service_name})
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

    tracer_provider = TracerProvider(resource=resource)
    metric_readers = []
    if endpoint:
        base = endpoint.rstrip("/")
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
        )
        metric_readers.append(
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics"))
        )

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))
    return PersistedTraceTracingInterceptor(
        tracer=tracer_provider.get_tracer(_INSTRUMENTATION_NAME)
    )


def tracer() -> Tracer:
    return trace.get_tracer(_INSTRUMENTATION_NAME)


def meter() -> Meter:
    return metrics.get_meter(_INSTRUMENTATION_NAME)


def current_traceparent() -> str | None:
    """Return only W3C trace identity; never persist baggage."""

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None):
    """Extract a saved W3C parent into an OpenTelemetry context."""

    if traceparent is None:
        return None
    return propagate.extract({"traceparent": traceparent})


__all__ = [
    "PersistedTraceTracingInterceptor",
    "configure_telemetry",
    "context_from_traceparent",
    "current_traceparent",
    "meter",
    "tracer",
]
