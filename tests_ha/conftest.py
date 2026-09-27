"""Simulation mod en rigtig HA-kerne. Kør separat (ikke i CI):

    pip install -r requirements-ha-test.txt
    pytest tests_ha -p no:cacheprovider -o asyncio_mode=auto
"""
import sys

import pytest

if sys.platform == "win32":
    # Windows-event-loopet bruger en intern socketpair, som pytest-socket blokerer
    import pytest_socket

    pytest_socket.disable_socket = lambda *a, **k: None
    pytest_socket.socket_allow_hosts = lambda *a, **k: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
