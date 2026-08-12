import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "users.py",
    'users = module.Users()\nbefore = users.get(1)\nusers.update(1, "New")\nresult = [before, users.get(1)]',
)
assert result == ["Old", "New"]
