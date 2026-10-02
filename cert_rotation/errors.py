class RotationError(Exception):
    """Generic orchestration / store error."""


class RollbackRefused(RotationError):
    """Raised when a manual rollback is unsafe unless explicitly forced."""
