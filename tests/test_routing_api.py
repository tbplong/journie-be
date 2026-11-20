import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.retrieval import get_connection
from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.routing import RoutingRequest
from app.services.retrieval.poi import Poi
from app.services.routing import service
from app.services.routing.osrm import road_km

H = 3600
START = {"lat": 10.7725, "lon": 106.6980}
OPEN_10_22 = {
    day: [["10:00", "22:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


def card(poi_id: str) -> dict:
    return {
        "id": poi_id, "name": poi_id, "kind": "x", "category": None, "ward": None,
        "address": None, "lat": None, "lon": None, "score": 0.5, "score_parts": {},
        "reasons": [], "flags": [], "price_raw": None, "opening_hours_raw": None,
        "website": None, "phone": None,
    }  # fmt: skip


def poi(poi_id: str, dlat: float, category: str = "food.restaurant", hours=OPEN_10_22) -> Poi:
    return Poi(
        id=poi_id,
        name=poi_id,
        kind="x",
        category=category,
        latitude=START["lat"] + dlat,
        longitude=START["lon"],
        opening_hours=hours,
    )


def request(places: list[str], start: str, end: str, **extra) -> RoutingRequest:
    return RoutingRequest.model_validate(
        {"places": [card(p) for p in places], "start": START, "start_at": start, "end_at": end}
        | extra
    )


SETTINGS = Settings(_env_file=None, routing_time_limit_seconds=1.0)  # no OSRM: estimates


def fake_db(monkeypatch: pytest.MonkeyPatch, pois: list[Poi]) -> None:
    async def fetch_by_ids(conn: object, ids: list[str]) -> dict[str, Poi]:
        return {p.id: p for p in pois if p.id in ids}

    monkeypatch.setattr(service, "fetch_by_ids", fetch_by_ids)


def plan(monkeypatch: pytest.MonkeyPatch, pois: list[Poi], body: RoutingRequest):
    fake_db(monkeypatch, pois)

    async def run():
        async with httpx.AsyncClient() as client:
            return await service.plan(None, client, SETTINGS, body)

    return asyncio.run(run())


# ---------------------------------------------------------------- day bounds and windows


def test_first_day_starts_at_start_and_last_day_ends_at_the_earlier_end() -> None:
    body = request(["a"], "2026-11-20T14:30", "2026-11-22T17:00")
    assert service.day_bounds(body) == [
        (14.5 * H, 21 * H),
        (24 * H + 8 * H, 24 * H + 21 * H),
        (48 * H + 8 * H, 48 * H + 17 * H),
    ]
    late = request(["a"], "2026-11-20T22:00", "2026-11-21T12:00", day_end="20:00")
    assert service.day_bounds(late)[0] == (22 * H, 22 * H)  # no time left: an empty day


def test_utc_times_are_read_in_ho_chi_minh_time() -> None:
    body = request(["a"], "2026-11-20T02:00:00Z", "2026-11-20T10:00:00Z")
    assert service.day_bounds(body) == [(9 * H, 17 * H)]
    assert body.start_at.isoformat() == "2026-11-20T09:00:00+07:00"


FRI, SUN, MON = 4, 6, 0


@pytest.mark.parametrize(
    ("hours", "first_weekday", "expected"),
    [
        (None, FRI, ((0, 20 * H),)),  # no hours: open all day, visit ends by day end
        ({"mon": [["08:00", "17:00"]]}, FRI, ()),  # closed on the trip day
        ({"fri": [["18:00", "02:00"]]}, FRI, ((18 * H, 20 * H),)),  # past midnight, cut at 21:00
        ({"sun": [["20:00", "03:00"]]}, MON, ((0, 2 * H),)),  # Sunday night runs into Monday
        (  # lunch and dinner shifts; the lunch window is 11:00-14:00 minus the 60 min visit
            {"sun": [["11:00", "14:00"], ["17:00", "22:00"]]},
            SUN,
            ((11 * H, 13 * H), (17 * H, 20 * H)),
        ),
        ({"fri": [["10:00", "10:30"]]}, FRI, ()),  # open for less than the visit
    ],
)
def test_windows(hours: dict | None, first_weekday: int, expected: tuple) -> None:
    assert service.windows(hours, H, [(0, 21 * H)], first_weekday) == expected


def test_windows_follow_each_trip_day() -> None:
    hours = {"fri": [["09:00", "12:00"]], "sun": [["09:00", "12:00"]]}  # closed Saturday
    bounds = [(8 * H, 21 * H), (24 * H + 8 * H, 24 * H + 21 * H), (48 * H + 8 * H, 48 * H + 21 * H)]
    assert service.windows(hours, H, bounds, FRI) == ((9 * H, 11 * H), (57 * H, 59 * H))


def test_walk_below_one_km_and_ride_above() -> None:
    assert service.leg(0.9).mode == "walk" and service.leg_seconds(0.9) == 720  # 4.5 km/h
    assert service.leg(1.0).mode == "ride" and service.leg_seconds(10) == 1800  # 20 km/h


# ---------------------------------------------------------------- OSRM


def with_osrm(handler, call):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def test_osrm_table_is_one_request_in_lon_lat_order() -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url)
        return httpx.Response(200, json={"code": "Ok", "distances": [[0, 1500], [1700, None]]})

    points = [(10.0, 106.0), (10.1, 106.1)]
    km = with_osrm(handler, lambda client: road_km(client, "http://osrm/", points, 1))
    assert km == [[0, 1.5], [1.7, None]]
    assert (
        len(seen) == 1
        and seen[0].path == "/table/v1/driving/106.000000,10.000000;106.100000,10.100000"
    )
    assert seen[0].params["annotations"] == "distance"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"code": "InvalidQuery"}),
        httpx.Response(200, json={"code": "Ok", "distances": [[0, 1]]}),  # wrong shape
        httpx.ReadTimeout("slow"),
    ],
)
def test_unusable_osrm_answers_give_none(answer) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    points = [(10.0, 106.0), (10.1, 106.1)]
    assert with_osrm(handler, lambda client: road_km(client, "http://osrm", points, 1)) is None


def test_a_missing_road_falls_back_to_an_estimate_for_that_pair_only() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "Ok", "distances": [[0, 2500], [None, 0]]})

    settings = Settings(_env_file=None, osrm_url="http://osrm")
    points = [(10.0, 106.0), (10.01, 106.0)]
    km, estimated = with_osrm(handler, lambda client: service.distances(client, settings, points))
    assert km[0][1] == 2.5 and estimated
    assert km[1][0] == pytest.approx(1.112 * service.DETOUR_FACTOR, rel=1e-3)


# ---------------------------------------------------------------- plan


def test_plan_orders_places_and_explains_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    pois = [
        poi("a", 0.004),
        poi("b", -0.003, "food.cafe", hours=None),
        poi("c", 0.002, "attraction.museum", hours={"mon": [["08:00", "17:00"]]}),
        Poi(id="e", name="e", kind="x"),  # no coordinates
        poi("f", 0.010),
    ]
    body = request(["a", "b", "c", "d", "e", "f"], "2026-11-20T09:00", "2026-11-20T18:00")
    result = plan(monkeypatch, pois, body)

    [day] = result.days
    assert str(day.date) == "2026-11-20"
    assert sorted(stop.poi.id for stop in day.stops) == ["a", "b", "f"]
    assert [(u.poi.id, u.reason) for u in result.unscheduled] == [
        ("c", "closed_on_trip_days"),
        ("d", "not_found"),
        ("e", "no_location"),
    ]
    previous_leave = body.start_at
    for stop in day.stops:
        assert stop.arrive >= previous_leave and stop.start >= stop.arrive
        assert (stop.leave - stop.start).total_seconds() == stop.visit_minutes * 60
        if stop.poi.id != "b":
            assert stop.start.hour >= 10  # a and f open at 10:00
        previous_leave = stop.leave
    assert day.return_leg is not None and previous_leave <= body.end_at
    # a, b and f are all food: a pair in a row cannot be avoided, so it is kept and reported
    assert result.warnings == [
        "travel_estimated",
        "hours_unknown:b",
        "not_adjacent:food:2026-11-20",
    ]
    assert result.meta.travel_source == "estimated"
    assert result.meta.visit_minutes_version == service.VISIT_MINUTES.version


def test_must_visit_is_kept_when_not_everything_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    # 09:00-11:30, 60 min restaurants opening 10:00, 4-6.5 km away: each fits alone, no two do.
    pois = [poi("near", 0.03), poi("far", -0.04), poi("must", 0.045)]
    body = request(
        ["near", "far", "must"], "2026-11-20T09:00", "2026-11-20T11:30", must_visit_ids=["must"]
    )
    result = plan(monkeypatch, pois, body)
    assert [stop.poi.id for stop in result.days[0].stops] == ["must"]
    assert {(u.poi.id, u.reason) for u in result.unscheduled} == {
        ("near", "no_time"),
        ("far", "no_time"),
    }


def test_each_day_gets_its_own_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    only_sunday = {"sun": [["09:00", "12:00"]]}
    pois = [
        poi("fri", 0.005, hours={"fri": [["09:00", "12:00"]]}),
        poi("sun", -0.005, hours=only_sunday),
    ]
    body = request(["sun", "fri"], "2026-11-20T08:00", "2026-11-22T20:00")
    result = plan(monkeypatch, pois, body)
    assert [[s.poi.id for s in day.stops] for day in result.days] == [["fri"], [], ["sun"]]
    assert result.days[1].return_leg is None
    assert str(result.days[2].stops[0].start) == "2026-11-22 09:00:00+07:00"


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("app.main.create_pool", lambda settings: None)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def no_connection() -> AsyncIterator[None]:
    yield None


BODY = {
    "places": [card("a")], "start": START,
    "start_at": "2026-11-20T09:00", "end_at": "2026-11-20T18:00",
}  # fmt: skip


def test_route_returns_the_itinerary(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db(monkeypatch, [poi("a", 0.004)])
    app.dependency_overrides[get_connection] = no_connection
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    response = client.post("/routing", json=BODY)
    assert response.status_code == 200, response.text
    assert response.json()["days"][0]["stops"][0]["poi"] == card("a")  # the card comes back as sent


def test_missing_database_is_a_problem_response(client: TestClient) -> None:
    response = client.post("/routing", json=BODY)
    assert response.status_code == 503
    assert response.json()["type"] == "/problems/retrieval-not-configured"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"places": [card("a"), card("a")]}, "same id twice"),
        ({"must_visit_ids": ["zzz"]}, "must_visit_ids not in places"),
        ({"end_at": "2026-11-20T08:00"}, "end_at must be after start_at"),
        ({"end_at": "2026-12-04T08:00"}, "more than 14 days"),
        ({"day_start": "21:00"}, "day_end must be after day_start"),
        ({"places": []}, "at least 1"),
        ({"places": [card(str(i)) for i in range(51)]}, "at most 50"),
    ],
)
def test_bad_requests_are_rejected(client: TestClient, change: dict, message: str) -> None:
    app.dependency_overrides[get_connection] = no_connection
    response = client.post("/routing", json=BODY | change)
    assert response.status_code == 422
    assert message in response.text


def test_nothing_to_schedule_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    result = plan(monkeypatch, [], request(["gone"], "2026-11-20T09:00", "2026-11-21T18:00"))
    assert [day.stops for day in result.days] == [[], []]
    assert [(u.poi.id, u.reason) for u in result.unscheduled] == [("gone", "not_found")]
    assert result.meta.travel_source == "estimated"  # no OSRM configured


def test_visit_minutes_use_taxonomy_categories() -> None:
    from tests.test_retrieval_taxonomy import TAXONOMY

    assert set(service.VISIT_MINUTES.by_category) <= TAXONOMY.categories
