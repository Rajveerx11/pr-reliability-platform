"""Supported JUnit and hostile XML regression tests."""

import pytest
from pr_reliability_evidence import MAX_REPORT_BYTES, ReportError, summarize_junit


def test_junit_counts_cases_not_untrusted_suite_aggregates():
    result = summarize_junit(b"""<testsuites><testsuite tests="9999" failures="0">
      <testcase name="pass" time="0.125"/>
      <testcase name="fail" time="1"><failure>private</failure></testcase>
      <testcase name="error"><error/></testcase>
      <testcase name="skip"><skipped/></testcase>
    </testsuite></testsuites>""")
    assert result == {"passed": 1, "failed": 2, "skipped": 1, "duration_ms": 1125}


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"broken",
        b"<testsuite>",
        b"<html/>",
        b'<!DOCTYPE testsuite [<!ENTITY x "secret">]><testsuite>&x;</testsuite>',
        b'<!DOCTYPE testsuite SYSTEM "file:///etc/passwd"><testsuite/>',
        "<!DOCTYPE testsuite><testsuite/>".encode("utf-16"),
        b'<testsuite><testcase time="nan"/></testsuite>',
        b'<testsuite><testcase time="inf"/></testsuite>',
        b'<testsuite><testcase time="-1"/></testsuite>',
        b'<testsuite><testcase time="100000000"/></testsuite>',
        b"<testsuite><testcase><testcase/></testcase></testsuite>",
        b"<testsuite>" + b"<x>" * 40 + b"</x>" * 40 + b"</testsuite>",
        b"<testsuite>" + b"<testcase/>" * 20_000 + b"</testsuite>",
        b"x" * (MAX_REPORT_BYTES + 1),
    ],
    ids=lambda raw: f"xml-{len(raw)}-{raw[:25].hex()}",
)
def test_malformed_oversized_and_hostile_xml_fail_safely(raw):
    with pytest.raises(ReportError, match="report_invalid"):
        summarize_junit(raw)
