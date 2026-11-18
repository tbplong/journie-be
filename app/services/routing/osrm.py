"""Road distances from an OSRM server's table service (one request for the whole N x N matrix).

http://project-osrm.org/docs/v5.24.0/api/#table-service
"""

import httpx

Point = tuple[float, float]  # (lat, lon)


async def road_km(
    client: httpx.AsyncClient, base_url: str, points: list[Point], timeout_s: float
) -> list[list[float | None]] | None:
    """km from points[i] to points[j] by road (None where OSRM finds no route), or None when the
    server cannot be used (unreachable, timeout, error or a malformed answer)."""
    coordinates = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)  # OSRM wants lon,lat
    try:
        response = await client.get(
            f"{base_url.rstrip('/')}/table/v1/driving/{coordinates}",
            params={"annotations": "distance"},
            timeout=timeout_s,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("code") != "Ok":
        return None
    distances = body.get("distances")
    if not isinstance(distances, list) or len(distances) != len(points):
        return None
    if any(not isinstance(row, list) or len(row) != len(points) for row in distances):
        return None
    return [
        [meters / 1000 if isinstance(meters, int | float) else None for meters in row]
        for row in distances
    ]
