import pytest

from rfqbot.store import Store


@pytest.fixture
def store():
    s = Store(None)
    yield s
    s.close()
