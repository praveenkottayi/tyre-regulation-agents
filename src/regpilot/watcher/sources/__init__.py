from regpilot.watcher.sources.base import RegulationSource, VersionRef
from regpilot.watcher.sources.eurlex import EurLexCellarSource


def default_sources() -> list[RegulationSource]:
    """Registered sources, tried in order. Add India AIS/BIS etc. here."""
    return [EurLexCellarSource()]


__all__ = ["RegulationSource", "VersionRef", "default_sources"]
