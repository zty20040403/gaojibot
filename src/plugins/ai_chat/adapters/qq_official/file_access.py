"""Authenticated console file descriptors (public download links are disabled)."""
from dataclasses import dataclass
from typing import AsyncIterator


class LinkError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    sha256: str
    size: int
    mime: str
    filename: str


@dataclass(repr=False)
class VerifiedStream:
    sha256: str
    size: int
    chunks: AsyncIterator[bytes]
