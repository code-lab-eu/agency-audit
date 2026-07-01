"""Pure-Python geometry helpers for discovery tiling.

Side-effect-free functions used to subdivide city viewports into grid cells,
breaking the Google Places Text Search 60-result-per-query cap.  No I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Approximate meters per degree of latitude (WGS-84, varies < 0.5 % across the
# full latitude range, so a single constant is fine for our tiling purposes).
_M_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class Rectangle:
    """Axis-aligned bounding box defined by its southwest and northeast corners.

    ``low_*`` is the southwest corner; ``high_*`` is the northeast corner.
    """

    low_lat: float
    low_lng: float
    high_lat: float
    high_lng: float

    @property
    def area(self) -> float:
        """Area in square decimal-degrees (primarily for test assertions)."""
        return (self.high_lat - self.low_lat) * (self.high_lng - self.low_lng)


def subdivide(rect: Rectangle) -> list[Rectangle]:
    """Split *rect* into four equal-area quadrants that tile the parent with no
    gaps or overlap.

    The four children are returned in row-major (north-to-south, west-to-east)
    order: NW, NE, SW, SE.
    """
    mid_lat = (rect.low_lat + rect.high_lat) / 2.0
    mid_lng = (rect.low_lng + rect.high_lng) / 2.0

    return [
        Rectangle(mid_lat, rect.low_lng, rect.high_lat, mid_lng),  # NW
        Rectangle(mid_lat, mid_lng, rect.high_lat, rect.high_lng),  # NE
        Rectangle(rect.low_lat, rect.low_lng, mid_lat, mid_lng),  # SW
        Rectangle(rect.low_lat, mid_lng, mid_lat, rect.high_lng),  # SE
    ]


def bbox_from_center(lat: float, lon: float, half_extent_m: float) -> Rectangle:
    """Return a square bounding box centred on (*lat*, *lon*) with each side
    measuring ``2 * half_extent_m`` metres.

    Latitude degrees are treated as constant (``_M_PER_DEG_LAT``).  Longitude
    degrees are scaled by ``cos(latitude)`` to account for meridian convergence.
    This is an approximation suitable only for the discovery fallback path
    (when geocoding a city to a viewport is unavailable).
    """
    d_lat = half_extent_m / _M_PER_DEG_LAT
    d_lng = half_extent_m / (_M_PER_DEG_LAT * math.cos(math.radians(lat)))

    return Rectangle(
        low_lat=lat - d_lat,
        low_lng=lon - d_lng,
        high_lat=lat + d_lat,
        high_lng=lon + d_lng,
    )


def is_saturated(result_count: int, threshold: int) -> bool:
    """True when *result_count* meets or exceeds *threshold*."""
    return result_count >= threshold
