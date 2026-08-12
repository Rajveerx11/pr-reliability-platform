import importlib.util
import sys
from pathlib import Path

support_path = Path(__file__).resolve().parents[2] / "verifier_support.py"
spec = importlib.util.spec_from_file_location("verifier_support", support_path)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
result = support.run_module_probe(
    Path(sys.argv[1]),
    "signatures.py",
    """
import hashlib
import hmac
secret = b"test-secret"
payload = b'{"action":"opened"}'
digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
result = [module.verify(secret, payload, f"sha256={digest}"), module.verify(secret, payload + b"x", f"sha256={digest}")]
""",
)
assert result == [True, False]
