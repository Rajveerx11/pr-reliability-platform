import hashlib
import hmac


def verify(secret, payload, signature):
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return expected == signature
