import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "deliveries.py",
    """
deliveries = module.Deliveries()
calls = []
def fail_after_side_effect():
    calls.append("sent")
    raise RuntimeError("connection dropped")
try:
    deliveries.process("delivery-1", fail_after_side_effect)
except RuntimeError:
    failed = True
else:
    failed = False
try:
    retry = deliveries.process("delivery-1", fail_after_side_effect)
except RuntimeError:
    retry = "raised"
result = {"failed": failed, "retry": retry, "calls": calls}
""",
)
assert result == {"failed": True, "retry": False, "calls": ["sent"]}
