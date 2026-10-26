"""Independent desktop/server instances; importing this package never starts a bot."""

from .instances import ConfigurationConflict, InstanceError, InstanceStore

__all__ = ["ConfigurationConflict", "InstanceError", "InstanceStore"]
