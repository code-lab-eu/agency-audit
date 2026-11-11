"""Website URL identity: when two discovered URLs are the same website.

Google Maps gives each branch of an agency its own link to the agency website,
with its own tracking parameters, so the same website arrives under many URLs.
Two values are kept per website:

- the *address* (:func:`clean_website_url`): the discovered URL without
  tracking parameters. The auditor fetches it, so it stays as discovered
  otherwise: the scheme and ``www.`` decide whether the site answers, and the
  SSL check reads the scheme.
- the *key* (:func:`website_key`): the identity used to match websites. It
  ignores the scheme, ``www.``, a default port, a trailing slash, the fragment
  and the order of the query parameters. It keeps the path and the other
  parameters, because one domain can host several websites
  (``engelvoelkers.com/be/nl`` is not ``engelvoelkers.com``) and a maps-only
  place is identified by its ``cid`` parameter.

Query segments are compared raw (never decoded or re-encoded), so that
migration ``007_add_website_url_key.sql`` computes the same key in SQL.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

#: Query parameters that track the visitor and do not change the page.
#: Compared lowercase; any name starting with ``utm_`` is also a tracker.
TRACKING_PARAMETERS = frozenset(
    {
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking(segment: str) -> bool:
    name = segment.split("=", 1)[0].lower()
    return name.startswith("utm_") or name in TRACKING_PARAMETERS


def _kept_segments(query: str) -> list[str]:
    return [s for s in query.split("&") if s and not _is_tracking(s)]


def clean_website_url(url: str) -> str:
    """Return *url* without tracking parameters and fragment, otherwise unchanged."""
    parts = urlsplit(url.strip())
    query = "&".join(_kept_segments(parts.query))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def website_key(url: str) -> str:
    """Return the identity of the website at *url*.

    Example: ``https://www.heylenvastgoed.be/?utm_campaign=gmb+hasselt`` and
    ``http://heylenvastgoed.be`` both give ``heylenvastgoed.be``.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:  # non-numeric or out-of-range port: keep it as written
        port = None
        host = parts.netloc.rsplit("@", 1)[-1].lower().removeprefix("www.")
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    segments = sorted(_kept_segments(parts.query))
    query = "?" + "&".join(segments) if segments else ""
    return f"{host}{path}{query}"


def is_maps_only_url(url: str) -> bool:
    """Return whether *url* is the placeholder stored for a place without a website."""
    parts = urlsplit(url)
    return (parts.hostname or "").lower() == "maps.google.com" and parts.query.startswith("cid=")
