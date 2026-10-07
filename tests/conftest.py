import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from reddit_research_mcp import server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_client():
    """Every test starts without a shared client and leaves none behind."""
    server.set_client(None)
    yield
    server.set_client(None)
