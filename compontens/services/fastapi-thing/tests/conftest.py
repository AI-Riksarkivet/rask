import pytest
from fastapi.testclient import TestClient
from fastapi_thing import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
