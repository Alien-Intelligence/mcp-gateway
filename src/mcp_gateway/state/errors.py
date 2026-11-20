"""The shared state backend failed."""


class StateUnavailableError(Exception):
    """A state operation failed, timed out or could not connect.

    The gateway answers 503 and never forwards the request: without the state it cannot tell
    whether a session belongs to the caller or whether a cached credential was revoked. The
    message names the failure class only, never a key or a value.
    """
