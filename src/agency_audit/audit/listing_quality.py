"""Listing quality assessment module.

Checks:
  - Structured data (schema.org Product, Place, RealEstateListing)
  - Image quality (presence of images on listings)
  - Description completeness
  - Price presence
  - Location presence
  - Property map (Google Maps embed, Leaflet, etc.)
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from agency_audit.audit.models import ListingQualityResult

logger = logging.getLogger(__name__)

# CSS selectors for listing quality checks
PRICE_SELECTORS = [
    ".price",
    ".property-price",
    ".listing-price",
    "[class*='price']",
    "[data-price]",
    ".cost",
    ".amount",
]

LOCATION_SELECTORS = [
    ".location",
    ".property-location",
    ".address",
    "[class*='location']",
    "[itemprop='address']",
    ".area",
]

IMAGE_SELECTORS = [
    ".property img",
    ".listing img",
    ".property-card img",
    ".listing-card img",
    "[class*='property'] img",
    "img[class*='property']",
]

DESCRIPTION_SELECTORS = [
    ".description",
    ".property-description",
    ".listing-description",
    "[class*='description']",
    "[itemprop='description']",
]

MAP_PATTERNS = [
    r"maps\.googleapis\.com",
    r"maps\.google\.com",
    r"leaflet",
    r"openstreetmap",
    r"mapbox",
    r"google\.com/maps/embed",
    r"leaflet-container",
]

# Schema.org types for real estate
REALESTATE_SCHEMA_TYPES = {
    "Product",
    "Place",
    "RealEstateListing",
    "Residence",
    "House",
    "Apartment",
    "SingleFamilyResidence",
    "Offer",
}


def _check_structured_data(html_text: str) -> bool:
    """Check for schema.org structured data."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)

    # Check JSON-LD
    scripts = tree.css('script[type="application/ld+json"]')
    for script in scripts:
        text = script.text()
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue

        items = []
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            if "@graph" in data:
                items.extend(data["@graph"])
            else:
                items.append(data)

        for item in items:
            if not isinstance(item, dict):
                continue
            schema_type = item.get("@type", "")
            if isinstance(schema_type, list):
                for t in schema_type:
                    if t in REALESTATE_SCHEMA_TYPES:
                        return True
            elif schema_type in REALESTATE_SCHEMA_TYPES:
                return True

    # Check microdata
    for scope in tree.css("[itemtype]"):
        item_type = scope.attributes.get("itemtype", "")
        for schema_type in REALESTATE_SCHEMA_TYPES:
            if schema_type.lower() in item_type.lower():
                return True

    return False


def _has_elements(tree, selectors: list[str]) -> bool:
    """Check if any of the CSS selectors match at least one element."""
    for selector in selectors:
        if tree.css_first(selector):
            return True
    return False


def _count_elements(tree, selectors: list[str]) -> int:
    """Count elements matching any of the selectors."""
    total = 0
    for selector in selectors:
        total += len(tree.css(selector))
    return total


def _check_map(html_text: str) -> bool:
    """Check for property map (Google Maps, Leaflet, etc.)."""
    html_lower = html_text.lower()
    for pattern in MAP_PATTERNS:
        if re.search(pattern, html_lower):
            return True

    # Check for map div containers (excluding sitemap, footer, etc.)
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)
    # Exclude common false positives
    EXCLUDE_MAP_PATTERNS = ["sitemap", "footer", "imagemap", "sitemap-map"]
    for el in tree.css("div"):
        class_name = el.attributes.get("class", "")
        id_name = el.attributes.get("id", "")
        combined = f"{class_name} {id_name}".lower()
        if "map" in combined:
            # Check it's not a false positive like "sitemap" or "imagemap"
            is_excluded = any(excl in combined for excl in EXCLUDE_MAP_PATTERNS)
            if not is_excluded:
                # Check it looks like a map container (not just text mentioning "map")
                map_classes = [c for c in combined.split() if "map" in c]
                for mc in map_classes:
                    if any(excl in mc for excl in EXCLUDE_MAP_PATTERNS):
                        continue
                    # A dedicated map class like "map", "map-container", "property-map"
                    if mc in ("map", "map-container", "property-map", "listing-map", "leaflet-container"):
                        return True
                    # Or contains map but not sitemap
                    if "map" in mc and "site" not in mc and "image" not in mc:
                        return True

    return False


async def assess_listing_quality(
    base_url: str,
    homepage_response: httpx.Response | None = None,
    listing_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ListingQualityResult:
    """Assess listing quality of a website.

    Checks the homepage and optionally a listing page for quality indicators.

    Args:
        base_url: Website URL.
        homepage_response: Optional pre-fetched homepage response.
        listing_url: Optional listing page URL to check additionally.
        client: Optional httpx.AsyncClient.

    Returns:
        ListingQualityResult with quality metrics.
    """
    result = ListingQualityResult()
    own_client = client is None and homepage_response is None
    if own_client:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)

    try:
        from selectolax.parser import HTMLParser

        # Get homepage HTML
        if homepage_response is None:
            if client is None:
                raise ValueError("Need client or response")
            homepage_response = await client.get(base_url)

        html_text = homepage_response.text
        tree = HTMLParser(html_text)

        # Structured data
        result.has_structured_data = _check_structured_data(html_text)

        # Images
        img_count = _count_elements(tree, IMAGE_SELECTORS)
        result.has_images = img_count >= 1

        # Prices
        result.has_prices = _has_elements(tree, PRICE_SELECTORS)

        # Locations
        result.has_locations = _has_elements(tree, LOCATION_SELECTORS)

        # Descriptions
        result.has_descriptions = _has_elements(tree, DESCRIPTION_SELECTORS)

        # Property map
        result.has_property_map = _check_map(html_text)

        # Also check listing page if provided
        if listing_url and client:
            try:
                listing_resp = await client.get(listing_url, timeout=15)
                if listing_resp.status_code < 400:
                    listing_tree = HTMLParser(listing_resp.text)
                    if not result.has_prices:
                        result.has_prices = _has_elements(listing_tree, PRICE_SELECTORS)
                    if not result.has_locations:
                        result.has_locations = _has_elements(listing_tree, LOCATION_SELECTORS)
                    if not result.has_images:
                        result.has_images = _has_elements(listing_tree, IMAGE_SELECTORS)
                    if not result.has_descriptions:
                        result.has_descriptions = _has_elements(
                            listing_tree, DESCRIPTION_SELECTORS
                        )
                    if not result.has_property_map:
                        result.has_property_map = _check_map(listing_resp.text)
            except Exception:
                pass

        # Compute quality score (0.0 - 1.0)
        checks = [
            result.has_structured_data,
            result.has_images,
            result.has_descriptions,
            result.has_prices,
            result.has_locations,
            result.has_property_map,
        ]
        result.quality_score = sum(checks) / len(checks) if checks else 0.0

    except Exception as exc:
        logger.warning("Listing quality assessment failed for %s: %s", base_url, exc)
    finally:
        if own_client and client:
            await client.aclose()

    return result
