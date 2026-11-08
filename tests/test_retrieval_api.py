from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.routes.retrieval import get_connection
from app.main import app
from app.schemas.retrieval import RetrievalRequest
from app.services.retrieval.service import request_seed
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
    assert empty["meta"]["retrieval_version"] == "retrieval-v1"


def test_request_validation(client: TestClient) -> None:
    app.dependency_overrides[get_connection] = no_connection
    response = client.post("/retrieval", json={"extraction": BASE, "limit": 99})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("must_visit", [f"place {i}" for i in range(21)]),
        ("interests", [f"interest {i}" for i in range(21)]),
        ("soft_constraints", [{"field": "other", "operator": "prefer", "value": "x"}] * 51),
    ],
)
def test_oversized_lists_are_rejected(client: TestClient, field: str, value: list) -> None:
    # Each entry would cost a query; the cap keeps one request from flooding the database.
    app.dependency_overrides[get_connection] = no_connection
    response = client.post("/retrieval", json={"extraction": {**BASE, field: value}})
    assert response.status_code == 422
    assert f"extraction.{field} has more than" in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("must_visit", ["x" * 201]),
        ("hard_constraints", [{"field": "other", "operator": "include", "value": "x" * 201}]),
    ],
)
def test_overlong_entries_are_rejected(client: TestClient, field: str, value: list) -> None:
    # Every entry is matched against every place in SQL; a huge string would hold the database.
    app.dependency_overrides[get_connection] = no_connection
    response = client.post("/retrieval", json={"extraction": {**BASE, field: value}})
    assert response.status_code == 422
    assert f"extraction.{field} has an entry longer than" in response.text


class FakePool:
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[object]:
        yield object()


def test_database_error_is_a_retryable_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timed_out(conn: object, body: object) -> None:
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr("app.api.routes.retrieval.retrieve", timed_out)
    monkeypatch.setattr(app.state, "db_pool", FakePool())
    response = client.post("/retrieval", json={"extraction": {**BASE, "interests": ["food"]}})
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "5"
    assert response.json()["type"] == "/problems/retrieval-db-unavailable"


def test_seed_defaults_to_the_extraction_and_can_be_overridden() -> None:
    def request(**fields: object) -> RetrievalRequest:
        return RetrievalRequest.model_validate(
            {"extraction": {**BASE, **fields.pop("ext", {})}, **fields}
        )

    cafe = request(ext={"interests": ["cafe"]})
    assert request_seed(cafe) == request_seed(request(ext={"interests": ["cafe"]}, offset=6))
    assert request_seed(cafe) != request_seed(request(ext={"interests": ["food"]}))
    assert request_seed(request(ext={"interests": ["cafe"]}, seed=7)) == 7
