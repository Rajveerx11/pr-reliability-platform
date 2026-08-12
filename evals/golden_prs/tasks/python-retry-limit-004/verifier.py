import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "retry.py",
    """
calls = []
def fail():
    calls.append(1)
    raise TimeoutError("provider timeout")
try:
    module.run_with_retry(fail, 3)
except TimeoutError as error:
    output = {"error": str(error), "calls": len(calls)}
else:
    output = {"error": None, "calls": len(calls)}
result = output
""",
)
assert result == {"error": "provider timeout", "calls": 3}
