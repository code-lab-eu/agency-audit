"""Pure-Python geometry helpers for discovery tiling.

Side-effect-free functions used to subdivide city viewports into grid cells,
breaking the Google Places Text Search 60-result-per-query cap.  No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass


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


def is_saturated(result_count: int, threshold: int) -> bool:
    """True when *result_count* meets or exceeds *threshold*."""
    return result_count >= threshold
