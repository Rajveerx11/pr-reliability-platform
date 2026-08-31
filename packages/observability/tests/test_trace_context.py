"""Tests for bounded W3C trace propagation."""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from pr_reliability_observability import context_from_traceparent, current_traceparent


def test_traceparent_round_trips_without_baggage() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("webhook") as source:
        traceparent = current_traceparent()

    restored = trace.get_current_span(context_from_traceparent(traceparent)).get_span_context()

    assert traceparent is not None
    assert restored.trace_id == source.get_span_context().trace_id
    assert len(traceparent) == 55


def test_missing_traceparent_stays_missing() -> None:
    assert context_from_traceparent(None) is None
