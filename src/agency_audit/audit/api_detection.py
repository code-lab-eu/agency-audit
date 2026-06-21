"""API endpoint detection module.

Looks for common real estate API patterns:
  - GraphQL endpoints (POST /graphql, /api/graphql)
  - REST API paths (/api/v1/, /api/listings, /wp-json/)
  - JSON-LD structured data (schema.org Product, Place, RealEstateListing)
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from agency_audit.audit.models import ApiDetectionResult

logger = logging.getLogger(__name__)

# Common API path patterns
API_PATH_PATTERNS = [
    r"/api/v\d+/",
    r"/api/listings",
    r"/api/properties",
    r"/api/search",
    r"/api/estate",
    r"/api/real-estate",
    r"/wp-json/wp/v\d+",
    r"/wp-json/",
    r"/graphql",
    r"/api/graphql",
    r"/.well-known/",
]

# GraphQL indicators in HTML
GRAPHQL_PATTERNS = [
    r"__NEXT_DATA__.*?graphql",
    r"application/graphql",
    r"query\s*\{.*?properties",
    r"query\s*\{.*?listings",
]

# JSON-LD schema.org types for real estate
REALESTATE_SCHEMA_TYPES = {
    "Product",
    "Place",
    "RealEsestateListing",
    "Residence",
    "House",
    "Apartment",
    "SingleFamilyResidence",
    "Offer",
}


def _check_jsonld_structured_data(html_text: str) -> tuple[bool, list[str]]:
    """Check for JSON-LD structured data in HTML.

    Returns:
        (has_realestate_jsonld, list of schema types found)
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)
    scripts = tree.css('script[type="application/ld+json"]')

    found_types = []
    for script in scripts:
        text = script.text()
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue

        # data can be a dict, list, or have @graph
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
                        found_types.append(t)
            elif schema_type in REALESTATE_SCHEMA_TYPES:
                found_types.append(schema_type)

    return bool(found_types), found_types


def _find_api_endpoints_in_html(html_text: str) -> list[str]:
    """Find API endpoint references in HTML (script srcs, fetch calls, etc.)."""
    endpoints = set()

    # Look for fetch/axios calls with API paths
    for pattern in API_PATH_PATTERNS:
        matches = re.findall(pattern, html_text, re.IGNORECASE)
        endpoints.update(matches)

    # Look for absolute API URLs
    api_url_patterns = [
        r'https?://[^/]+/api/v\d+/',
        r'https?://[^/]+/api/listings',
        r'https?://[^/]+/api/properties',
        r'https?://[^/]+/graphql',
    ]
    for pattern in api_url_patterns:
        matches = re.findall(pattern, html_text, re.IGNORECASE)
        endpoints.update(matches)

    return sorted(endpoints)


async def _probe_graphql(
    base_url: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Probe for a GraphQL endpoint by sending a simple introspection query."""
    graphql_paths = ["/graphql", "/api/graphql", "/query"]
    for path in graphql_paths:
        url = f"{base_url.rstrip('/')}{path}"
        try:
            resp = await client.post(
                url,
                json={"query": "{__typename}"},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code in (200, 400):
                try:
                    data = resp.json()
                    if "data" in data or "errors" in data:
                        return url
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
    return None


async def detect_api(
    base_url: str,
    response: httpx.Response | None = None,
    client: httpx.AsyncClient | None = None,
) -> ApiDetectionResult:
    """Detect API endpoints on a website.

    Args:
        base_url: Website URL to check.
        response: Optional pre-fetched homepage response.
        client: Optional httpx.AsyncClient for making requests.

    Returns:
        ApiDetectionResult with detected API info.
    """
    result = ApiDetectionResult()
    own_client = client is None and response is None
    if own_client:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)

    try:
        if response is None:
            if client is None:
                raise ValueError("Either response or client must be provided")
            response = await client.get(base_url)

        html_text = response.text

        # Check for JSON-LD structured data
        has_jsonld, schema_types = _check_jsonld_structured_data(html_text)
        if has_jsonld:
            result.detected = True
            result.api_type = "json-ld"
            result.endpoints_found.extend(schema_types)

        # Find API endpoints in HTML
        endpoints = _find_api_endpoints_in_html(html_text)
        if endpoints:
            result.detected = True
            if result.api_type is None:
                result.api_type = "rest"
            result.endpoints_found.extend(endpoints)

        # Check for GraphQL patterns in HTML
        for pattern in GRAPHQL_PATTERNS:
            if re.search(pattern, html_text, re.IGNORECASE):
                result.detected = True
                if result.api_type != "graphql":
                    result.api_type = "graphql"
                result.endpoints_found.append("graphql-pattern")
                break

        # Probe for GraphQL endpoint
        if client is not None:
            graphql_url = await _probe_graphql(base_url, client)
            if graphql_url:
                result.detected = True
                result.api_type = "graphql"
                result.api_url = graphql_url
                result.endpoints_found.append(graphql_url)

    except Exception as exc:
        logger.warning("API detection failed for %s: %s", base_url, exc)
    finally:
        if own_client and client:
            await client.aclose()

    return result
