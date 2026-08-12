import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "paths.py",
    """
import tempfile
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp) / "run"
    root.mkdir()
    inside = module.safe_artifact_path(root, "report.json") == root / "report.json"
    try:
        module.safe_artifact_path(root, "../run-private/secret.txt")
    except ValueError:
        escaped = False
    else:
        escaped = True
result = {"inside": inside, "escaped": escaped}
""",
)
assert result == {"inside": True, "escaped": False}
