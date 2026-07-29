class SongDNAError(Exception):
    """Base exception for framework failures."""


class ValidationError(SongDNAError):
    """Raised when style or song DNA violates the framework contract."""

