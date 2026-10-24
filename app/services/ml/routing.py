"""Delivery route optimization (TSP over a driver's stops).

OR-Tools solves it exactly-ish when installed; otherwise a deterministic pure-Python
nearest-neighbour + 2-opt fallback. CPU-bound work is offloaded with asyncio.to_thread
so it never blocks the event loop. Distances are haversine km (good enough for
intra-city routing; swap for a real travel matrix when the Routes API is wired).
"""
import asyncio
import math
import time

_EARTH_KM = 6371.0
MAX_STOPS = 500  # per optimisation call; larger days must be split per driver
_TWO_OPT_BUDGET_S = 2.0  # hard wall-clock cap on the pure-Python local search


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(h))


def _route_length(order: list[int], coords: list[tuple[float, float]]) -> float:
    return sum(haversine_km(coords[order[i]], coords[order[i + 1]]) for i in range(len(order) - 1))


def _nearest_neighbour(coords: list[tuple[float, float]], start: int = 0) -> list[int]:
    n = len(coords)
    unvisited = set(range(n))
    order = [start]
    unvisited.discard(start)
    while unvisited:
        last = order[-1]
        nxt = min(unvisited, key=lambda j: haversine_km(coords[last], coords[j]))
        order.append(nxt)
        unvisited.discard(nxt)
    return order


def _two_opt(
    order: list[int], coords: list[tuple[float, float]], budget_s: float = _TWO_OPT_BUDGET_S
) -> list[int]:
    """Deterministic 2-opt local search on an OPEN path, first node (start) fixed.

    Distances are precomputed once and each candidate move is scored by its O(1) edge
    delta (was a full O(n) route re-sum per candidate → O(n^4); 200 stops took ~28s).
    A wall-clock budget bounds the worst case; the best route so far is returned."""
    n = len(order)
    if n < 4:
        return order[:]
    d = [[haversine_km(coords[a], coords[b]) for b in range(len(coords))] for a in range(len(coords))]
    best = order[:]
    deadline = time.monotonic() + budget_s
    improved = True
    while improved and time.monotonic() < deadline:
        improved = False
        for i in range(1, n - 1):
            a, b = best[i - 1], best[i]
            for k in range(i + 1, n):
                c = best[k]
                if k == n - 1:  # reversing the tail: only edge (a,b) -> (a,c) changes
                    delta = d[a][c] - d[a][b]
                else:
                    e = best[k + 1]
                    delta = d[a][c] + d[b][e] - d[a][b] - d[c][e]
                if delta < -1e-9:
                    best[i : k + 1] = best[i : k + 1][::-1]
                    improved = True
                    a, b = best[i - 1], best[i]
            if time.monotonic() >= deadline:
                break
    return best


def _solve_pure(coords: list[tuple[float, float]]) -> list[int]:
    return _two_opt(_nearest_neighbour(coords, 0), coords)


def _solve_ortools(coords: list[tuple[float, float]]) -> list[int] | None:
    """Exact-ish OR-Tools TSP; returns None if the lib is unavailable."""
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except Exception:
        return None
    n = len(coords)
    scale = 1000  # metres-ish integer cost matrix
    matrix = [[int(haversine_km(coords[i], coords[j]) * scale) for j in range(n)] for i in range(n)]
    manager = pywrapcp.RoutingIndexManager(n, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def _cost(a, b):
        return matrix[manager.IndexToNode(a)][manager.IndexToNode(b)]

    idx = routing.RegisterTransitCallback(_cost)
    routing.SetArcCostEvaluatorOfAllVehicles(idx)
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 3
    sol = routing.SolveWithParameters(params)
    if sol is None:
        return None
    order, node = [], routing.Start(0)
    while not routing.IsEnd(node):
        order.append(manager.IndexToNode(node))
        node = sol.Value(routing.NextVar(node))
    return order


async def optimize_route(points: list[dict], start: dict | None = None) -> dict:
    """Order `points` (each {id, lat, lng}) into a short delivery route.

    Points without usable coords keep their input order (appended after the routed
    ones). Returns {ordered: [{id, seq}], total_distance_km}.
    """
    if len(points) > MAX_STOPS:
        raise ValueError(f"too many stops ({len(points)} > {MAX_STOPS}); optimise per driver")
    routable = [p for p in points if p.get("lat") is not None and p.get("lng") is not None]
    unroutable = [p for p in points if p.get("lat") is None or p.get("lng") is None]
    if len(routable) <= 2:
        ordered = routable + unroutable
        coords = [(float(p["lat"]), float(p["lng"])) for p in routable]
        dist = _route_length(list(range(len(routable))), coords) if len(routable) > 1 else 0.0
        return _result(ordered, dist)

    coords = [(float(p["lat"]), float(p["lng"])) for p in routable]
    if start is not None and start.get("lat") is not None:
        coords = [(float(start["lat"]), float(start["lng"])), *coords]
        order = await asyncio.to_thread(_solve, coords)
        order = [i for i in order if i != 0]  # drop the virtual start node
        seq = [routable[i - 1] for i in order]
    else:
        order = await asyncio.to_thread(_solve, coords)
        seq = [routable[i] for i in order]
    total = _route_length(
        list(range(len(seq))), [(float(p["lat"]), float(p["lng"])) for p in seq]
    )
    return _result(seq + unroutable, total)


def _solve(coords: list[tuple[float, float]]) -> list[int]:
    return _solve_ortools(coords) or _solve_pure(coords)


def _result(ordered_points: list[dict], total_km: float) -> dict:
    return {
        "ordered": [{"id": p["id"], "seq": i + 1} for i, p in enumerate(ordered_points)],
        "total_distance_km": round(total_km, 3),
    }
