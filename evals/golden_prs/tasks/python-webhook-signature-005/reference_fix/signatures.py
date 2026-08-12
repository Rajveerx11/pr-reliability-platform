import hashlib
import hmac


def verify(secret, payload, signature):
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)
