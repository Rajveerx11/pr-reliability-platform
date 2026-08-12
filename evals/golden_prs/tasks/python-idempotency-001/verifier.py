import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "checkout.py",
    """
class Gateway:
    def __init__(self):
        self.charges = []
    def charge(self, amount):
        self.charges.append(amount)
        return f"charge-{len(self.charges)}"
gateway = Gateway()
checkout = module.Checkout(gateway)
first = checkout.charge("stable-key", 2500)
second = checkout.charge("stable-key", 2500)
result = {"same": first == second, "charges": gateway.charges}
""",
)
assert result == {"same": True, "charges": [2500]}
