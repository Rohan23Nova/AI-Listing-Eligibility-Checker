"""
tests/conftest.py — Shared pytest fixtures.
"""
import pytest
import httpx


@pytest.fixture
def anyio_backend():
    return "asyncio"
