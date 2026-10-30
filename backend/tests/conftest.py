import pytest

from m2s.config import settings


@pytest.fixture(autouse=True)
def _heuristic_hands_by_default(monkeypatch, request):
    """Unit tests must not depend on the optional piano_svsep environment."""
    if "svsep" not in request.keywords:
        monkeypatch.setenv("M2S_HAND_MODEL", "heuristic")
    settings.cache_clear()
    yield
    settings.cache_clear()
