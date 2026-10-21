from pathlib import Path

import pytest

from regpilot.watcher.sources.base import VersionRef

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / f"{name}.xhtml").read_bytes()


class FakeSource:
    """Stands in for EUR-Lex: two R117 versions, served from fixtures. Counts
    fetches so tests can prove the watcher did NOT re-download."""

    name = "eurlex"  # serves real EUR-Lex fixture content (the impact judge relies on the source)

    def __init__(self):
        self.content = {"42016X0812(01)": fixture_bytes("r117_2016"), "42025X1453": fixture_bytes("r117_2025")}
        self.fetches: list[str] = []

    def discover(self, regulation):
        if regulation != "R117":
            return []
        return [
            VersionRef(regulation="R117", source="eurlex", source_id="42025X1453", date="2025-08-07",
                       title="UN Regulation No 117 (2025)", url="https://example.test/2025"),
            VersionRef(regulation="R117", source="eurlex", source_id="42016X0812(01)", date="2016-08-12",
                       title="Regulation No 117 (2016)", url="https://example.test/2016"),
        ]

    def fetch(self, ref, language="eng"):
        self.fetches.append(ref.source_id)
        return self.content[ref.source_id]


@pytest.fixture
def source():
    return FakeSource()


@pytest.fixture
def doc2025():
    from regpilot.watcher.parsers import oj_xhtml

    return oj_xhtml.parse(fixture_bytes("r117_2025"))


@pytest.fixture
def doc2016():
    from regpilot.watcher.parsers import oj_xhtml

    return oj_xhtml.parse(fixture_bytes("r117_2016"))
