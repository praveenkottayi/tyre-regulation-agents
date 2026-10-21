"""Source interface. Add a new jurisdiction (e.g. India AIS/BIS) by implementing
RegulationSource and registering it in sources/__init__.py:default_sources()."""

from typing import Protocol

from pydantic import BaseModel


class VersionRef(BaseModel):
    """One published version of a regulation, as found by discovery."""

    regulation: str  # "R117"
    source: str  # "eurlex"
    source_id: str  # CELEX, e.g. "42025X1453"
    date: str  # publication date, YYYY-MM-DD
    title: str
    url: str  # human-readable link


class RegulationSource(Protocol):
    name: str

    def discover(self, regulation: str) -> list[VersionRef]:
        """All known versions, newest first."""
        ...

    def fetch(self, ref: VersionRef, language: str) -> bytes:
        """Raw document content (e.g. XHTML) in one language."""
        ...
