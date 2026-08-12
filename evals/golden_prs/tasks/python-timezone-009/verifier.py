import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "timestamps.py",
    'parsed = module.parse_timestamp("2026-08-12T07:00:00Z")\nresult = [parsed.isoformat(), parsed.utcoffset().total_seconds() if parsed.tzinfo else None]',
)
assert result == ["2026-08-12T07:00:00+00:00", 0.0]
