"""TLS failures that no retry can fix."""

import ssl


def is_certificate_failure(exc: BaseException) -> bool:
    """True when an error was caused by a failed certificate verification.

    A server certificate the gateway does not trust is a configuration error: retrying the same
    handshake cannot succeed, so callers answer it at once instead of retrying.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False
