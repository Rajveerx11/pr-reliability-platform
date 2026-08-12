import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "money.py",
    'result = [module.to_cents("10.035"), module.to_cents("0.005"), module.to_cents("12.30")]',
)
assert result == [1004, 1, 1230]
