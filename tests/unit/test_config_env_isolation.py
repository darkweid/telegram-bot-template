import os

from src.main.config import WebServerConfig


def test_config_env_is_hidden_from_the_unit_suite() -> None:
    """The autouse fixture in conftest is what keeps every other unit test
    independent of the developer's shell; prove it actually clears the keys."""
    assert "DEBUG" not in os.environ
    assert "WEB_SERVER_PORT" not in os.environ
    # TESTING survives: it is what points get_settings() at .env.test.
    assert os.environ.get("TESTING") == "true"


def test_a_section_built_directly_ignores_a_shell_value() -> None:
    assert WebServerConfig().WEB_SERVER_PORT == 8080
