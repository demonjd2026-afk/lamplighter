import pytest


def pytest_addoption(parser):
    parser.addoption("--e2e", action="store_true", default=False, help="also run the end-to-end tests (slow)")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: starts real processes; run with --e2e")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--e2e"):
        return
    skip = pytest.mark.skip(reason="end-to-end test: run with --e2e")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)
