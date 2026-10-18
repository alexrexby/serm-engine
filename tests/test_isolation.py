"""The test session itself must be isolated from the real installation (see conftest.py)."""
import socket

import pytest


def test_real_env_file_is_not_visible():
    import config.settings as settings
    assert not settings.Settings.env_path.exists()
    assert settings.load_settings().llm_api_key.get_secret_value() == ""


def test_external_network_is_blocked():
    with pytest.raises(RuntimeError, match="must not use the network"):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
