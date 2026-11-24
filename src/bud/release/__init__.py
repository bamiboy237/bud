"""Standalone packaging, backup, LaunchAgent management, and atomic self-updates."""


class ReleaseError(Exception):
    """Base error for Bud packaging, backup, service, and update operations."""


class BackupError(ReleaseError):
    """Backup creation, verification, or restore failed."""


class ServiceError(ReleaseError):
    """Managing or verifying the background Bud service failed."""


class UpdateError(ReleaseError):
    """Downloading, building, verifying, or activating a release failed."""


class UpdateRollbackError(UpdateError):
    """An update failed verification and automatically rolled back."""

    def __init__(self, message: str, *, rollback_ok: bool) -> None:
        self.rollback_ok = rollback_ok
        super().__init__(message)
