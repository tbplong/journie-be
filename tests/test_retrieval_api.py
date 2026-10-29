from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.routes.retrieval import get_connection
from app.main import app
from tests.test_retrieval_names_interests import BASE


async def no_connection() -> AsyncIterator[None]:
    yield None  # the statuses tested here return before any query


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Never open the real pool from .env in unit tests.
    monkeypatch.setattr("app.main.create_pool", lambda settings: None)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_missing_database_is_a_problem_response(client: TestClient) -> None:
    response = client.post("/retrieval", json={"extraction": {**BASE, "interests": ["food"]}})
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "/problems/retrieval-not-configured"


def test_statuses_that_need_no_query(client: TestClient) -> None:
    app.dependency_overrides[get_connection] = no_connection
    other_city = client.post(
        "/retrieval", json={"extraction": {**BASE, "destination": "Hanoi", "interests": ["food"]}}
    ).json()
    assert other_city["status"] == "unsupported_destination"
    assert other_city["sections"] == [] and other_city["warnings"]

    empty = client.post("/retrieval", json={"extraction": {**BASE, "destination": None}}).json()
    assert empty["status"] == "empty_extraction"
    assert empty["meta"]["retrieval_version"] == "retrieval-v0"


def test_request_validation(client: TestClient) -> None:
    app.dependency_overrides[get_connection] = no_connection
    response = client.post("/retrieval", json={"extraction": BASE, "limit": 99})
    assert response.status_code == 422
