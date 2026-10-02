"""Shared fixtures."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))


@pytest.fixture
def rm():
    """Builders of reMarkable documents (skips the test without pymupdf / rmscene)."""
    pytest.importorskip("pymupdf")
    pytest.importorskip("rmscene")
    import rmdoc_builder

    return rmdoc_builder
