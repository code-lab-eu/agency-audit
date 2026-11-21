"""Property count estimation module.

Estimates the number of listed properties from three sources:
  1. The homepage and a listing page: count phrases ("454 biens"), pagination
     (last page number times the listings on the page), listing elements, and
     totals in embedded JSON
  2. Sitemaps: the URLs of listing detail pages
  3. The WordPress REST API, as a cross-check of the sitemap count

The detection is generic: it relies on a multilingual real estate vocabulary
and on the structure of URLs, never on the markup of a specific website or
platform. The result is an estimate.
"""

from __future__ import annotations

import gzip
import html
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import httpx

from agency_audit.audit.models import PropertyCountResult
from agency_audit.config import settings

logger = logging.getLogger(__name__)


def _fold(text: str) -> str:
    """Lowercase ``text`` and strip diacritics, so "Häuser" matches "hauser"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


# Words in URL path segments that mark real estate listings, in the languages
# of the audited countries. A segment matches when one of its words is in the
# set: "te-koop" matches on "koop", "a-vendre" on "vendre".
LISTING_TERMS = frozenset(
    _fold(term)
    for term in (
        # English
        "property", "properties", "listing", "listings", "estate", "realestate",
        "homes", "houses", "apartments", "flats", "villas", "sale", "sales", "buy",
        "rent", "rental", "rentals", "lettings", "offer", "offers",
        # Dutch
        "koop", "kopen", "huur", "huren", "aanbod", "pand", "panden", "woning",
        "woningen", "vastgoed", "nieuwbouw", "huizen", "appartementen",
        # French
        "vendre", "vente", "louer", "location", "achat", "acheter", "bien", "biens",
        "immobilier", "annonce", "annonces", "logements", "maisons",
        # German
        "kaufen", "kauf", "mieten", "miete", "immobilie", "immobilien", "objekt",
        "objekte", "expose", "exposé", "wohnungen", "häuser", "haeuser",
        # Italian
        "vendita", "affitto", "immobile", "immobili", "annuncio", "annunci",
        "appartamenti",
        # Spanish and Portuguese
        "venta", "alquiler", "comprar", "alquilar", "inmueble", "inmuebles",
        "propiedad", "propiedades", "pisos", "viviendas", "venda", "arrendamento",
        "arrendar", "aluguer", "aluguel", "imóvel", "imóveis",
        # Polish, Czech, Slovak
        "sprzedaż", "wynajem", "nieruchomości", "oferta", "oferty", "mieszkania",
        "domy", "prodej", "pronájem", "nemovitosti", "byty", "predaj", "prenájom",
        "nehnuteľnosti",
        # Hungarian, Romanian
        "eladó", "kiadó", "ingatlan", "ingatlanok", "vânzare", "închiriere",
        "imobiliare", "proprietăți", "oferte", "apartamente",
        # Bulgarian (Latin and Cyrillic)
        "imoti", "imot", "prodazhba", "prodajba", "naem", "имоти", "имот",
        "продажба", "наем", "оферти",
        # Croatian, Serbian, Bosnian, Slovenian, Macedonian
        "prodaja", "najam", "iznajmljivanje", "nekretnine", "nekretnina",
        "nepremičnine", "oddaja", "stanovi", "некретнине", "продаја", "недвижности",
        # Greek
        "akinita", "ακίνητα", "πώληση", "ενοικίαση",
        # Swedish, Danish, Norwegian, Icelandic
        "salu", "bostäder", "bostad", "hyra", "salg", "leje", "boliger", "bolig",
        "ejendomme", "ejendom", "utleie", "eiendom", "eiendommer", "fasteignir",
        # Finnish, Estonian, Latvian, Lithuanian
        "myytävät", "vuokrattavat", "asunnot", "kohteet", "myynti", "vuokra",
        "kinnisvara", "müük", "nekustamie", "īpašumi", "pārdod", "nekilnojamasis",
        "pardavimas", "nuoma", "butai",
        # Turkish, Albanian
        "satılık", "kiralık", "emlak", "ilan", "ilanlar", "shitje", "qira", "prona",
        # Russian, Ukrainian (Latin and Cyrillic)
        "nedvizhimost", "prodazha", "arenda", "kvartiry", "недвижимость", "продажа",
        "аренда", "квартиры", "нерухомість", "продаж", "оренда",
    )
)  # fmt: skip

# Words that mark a search page; a search page is a listing page, but its URLs
# in a sitemap are filter pages, not listings.
SEARCH_TERMS = frozenset(
    _fold(term)
    for term in (
        "search", "zoeken", "recherche", "suche", "ricerca", "busqueda", "búsqueda",
        "szukaj", "hledat", "hľadať", "căutare", "keresés", "pretraga", "haku",
    )
)  # fmt: skip

# Nouns that follow the total on a listing page: "454 biens", "1.250 Treffer".
COUNT_NOUNS = (
    # Listings
    "properties", "property", "listings", "homes", "biens", "bien", "propriétés",
    "annonces", "logements", "panden", "woningen", "objecten", "immobilien",
    "objekte", "objekt", "immobili", "annunci", "inmuebles", "propiedades",
    "viviendas", "imóveis", "nieruchomości", "ofert", "ogłoszeń", "nemovitostí",
    "nehnuteľností", "ingatlan", "proprietăți", "anunțuri", "imoti", "имота",
    "имоти", "обяви", "nekretnina", "nekretnine", "nepremičnin", "ακίνητα",
    "bostäder", "boliger", "kohdetta", "ilan",
    # Search results
    "results", "result", "résultats", "resultaten", "ergebnisse", "treffer",
    "risultati", "resultados", "wyników", "výsledků", "výsledkov", "találat",
    "rezultate", "rezultati", "резултата", "tulosta", "resultat", "resultater",
    "sonuç",
)  # fmt: skip

_NUMBER = r"(\d{1,3}(?:[.,   ]\d{3})+|\d+)"

LISTING_COUNT_PATTERNS = [
    # "1,250 properties", "454 bien(s)", "6 259 panden"
    re.compile(rf"{_NUMBER}\s+(?:{'|'.join(COUNT_NOUNS)})(?!\w)", re.IGNORECASE),
    # "1 - 36 of 6,259", "1-20 van 454", "1–24 sur 1 250"
    re.compile(
        rf"\d+\s*[-–]\s*\d+\s+(?:of|van|sur|de|von|di|z|ze|od|av|af|из|от)\s+{_NUMBER}(?!\d)",
        re.IGNORECASE,
    ),
]

# A currency before a number makes it a price
CURRENCY = re.compile(
    r"[€$£¥₺₽₴]|\b(?:eur|chf|gbp|usd|pln|zł|czk|kč|huf|ron|lei|bgn|лв|sek|nok|dkk|kr)\b", re.I
)

# JSON totals in embedded scripts (no thousands separators in JSON numbers)
JSON_COUNT_PATTERNS = [
    re.compile(r'"total(?:Count|Results|Items|Properties|Listings)"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'"count"\s*:\s*(\d+)', re.IGNORECASE),
    re.compile(r'"total"\s*:\s*(\d+)', re.IGNORECASE),
]

# CSS selectors for property listing items
LISTING_ITEM_SELECTORS = [
    ".property",
    ".listing",
    ".listing-item",
    ".property-item",
    ".property-card",
    ".listing-card",
    ".estate-item",
    ".imot",
    ".imoti",
    "[data-property-id]",
    "[data-listing-id]",
    ".offer-item",
    ".result-item",
    ".search-result",
]

# Query parameters and path segments that carry a page number in pagination links
PAGE_PARAMETERS = frozenset(
    {
        "page", "p", "pg", "pag", "pagina", "paged", "pageno", "pagenum", "currentpage",
        "seite", "strona", "stran", "stranica", "sayfa", "sivu", "side", "sida", "oldal",
    }
)  # fmt: skip
PAGE_PATH = re.compile(r"/(?:" + "|".join(sorted(PAGE_PARAMETERS)) + r")[/-](\d+)/?$", re.I)

# A pagination link beyond this page number is not a page number.
MAX_PAGE_NUMBER = 10_000

# A first path segment that is a language code: "nl", "fr", "en-gb", "pt_BR";
# not "te-koop".
LANGUAGE_SEGMENT = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?", re.IGNORECASE)

# A last path segment that names a server-side script: "property.php", "detail.aspx".
SCRIPT_SEGMENT = re.compile(r"\.(?:php\d?|aspx?|jsp|cfm|cgi|pl)$", re.IGNORECASE)

# The last path segment of a listing identifies it; a website may list the same
# listing twice, such as under "for sale" and "new". A last segment that ends
# more URLs is a category or filter value, such as "huis" in "/te-koop/alle/huis".
MAX_LISTING_SEGMENT_REPEATS = 2

# Within a listing section of a website, listings are the deepest URL level that
# holds at least this share of the largest level, and at least two URLs unless
# it is the largest. Shallower levels are category and filter pages, such as
# "/te-koop/gent" above "/te-koop/gent/huis/x".
DETAIL_LEVEL_SHARE = 0.1

# Maximum number of candidate listing pages fetched for one website
MAX_LISTING_PAGES = 3

# Maximum number of sitemap documents fetched for one website, sitemap indexes
# included.
MAX_SITEMAPS = 50

# Estimates at or above this confidence count the listings themselves; the
# lower ones are partial counts, such as the listings on one page.
STRONG_CONFIDENCE = 0.6

_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.DOTALL)


def _parse_count(raw: str) -> int | None:
    """Parse a matched count string into an int, tolerating thousands separators.

    Returns None if the string contains no digits.
    """
    digits = re.sub(r"[.,\s  ]", "", raw)
    if not digits.isdigit():
        return None
    return int(digits)


def _words(segment: str) -> list[str]:
    """Split a URL path segment into folded words."""
    return [word for word in re.split(r"[\W_]+", _fold(segment)) if word]


def _is_listing_segment(segment: str) -> bool:
    return any(word in LISTING_TERMS for word in _words(segment))


def _split_url(url: str) -> tuple[str, tuple[str, ...]]:
    """Split a URL into its language prefix and its other path segments.

    Behind a script, such as ``/property.php?id=1``, the query string identifies
    the page and is the last segment. Elsewhere it holds filters, sorting or a
    page number (``/te-koop?page=2``) and is dropped.

    Returns:
        (language, segments) — language is "" for a URL without a language prefix.
    """
    parts = urlsplit(url.strip())
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    language = ""
    if segments and LANGUAGE_SEGMENT.fullmatch(segments[0]):
        language = segments.pop(0).lower()
    if parts.query and segments and SCRIPT_SEGMENT.search(segments[-1]):
        segments.append("?" + parts.query)
    return language, tuple(segments)


def _site_host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _listing_detail_paths(urls: Iterable[str]) -> set[str]:
    """Select the URLs of listing detail pages and return their paths.

    A URL belongs to a listing section when a path segment other than the last
    one contains a listing term: ``/te-koop/gent/huis/x`` is in section
    ``/te-koop``. A listing is a leaf: no other URL lies below it, and its last
    segment, which identifies it, occurs in at most
    ``MAX_LISTING_SEGMENT_REPEATS`` URLs. Category and filter pages fail one of
    the two: ``/te-koop/gent`` has listings below it, and the ``huis`` of
    ``/te-koop/alle/huis`` ends thousands of filter URLs. Of the remaining URLs
    of a section, the deepest level that holds at least ``DETAIL_LEVEL_SHARE``
    of the largest level holds the listings.

    A website that translates its paths lists each listing once per language
    (``/nl/te-koop/x``, ``/fr/a-vendre/x``), so the URLs are counted per language
    prefix and the language with the most listings is kept. URLs without a
    prefix are a language of their own.

    Returns:
        The paths, without language prefix, of the listings in that language.
    """
    # language -> {segments: section}
    candidates: dict[str, dict[tuple[str, ...], tuple[str, ...]]] = {}
    for url in urls:
        language, segments = _split_url(url)
        for index, segment in enumerate(segments[:-1]):
            if _is_listing_segment(segment):
                candidates.setdefault(language, {})[segments] = segments[: index + 1]
                break

    sections: dict[tuple[str, tuple[str, ...]], dict[int, set[str]]] = {}
    for language, entries in candidates.items():
        last_segments = Counter(segments[-1] for segments in entries)
        parents = {segments[:end] for segments in entries for end in range(1, len(segments))}
        for segments, section in entries.items():
            if segments in parents or last_segments[segments[-1]] > MAX_LISTING_SEGMENT_REPEATS:
                continue
            levels = sections.setdefault((language, section), {})
            levels.setdefault(len(segments), set()).add("/".join(segments))

    by_language: dict[str, set[str]] = {}
    for (language, _section), levels in sections.items():
        largest = max(len(paths) for paths in levels.values())
        depth = max(
            level
            for level, paths in levels.items()
            if len(paths) == largest or len(paths) >= max(2, DETAIL_LEVEL_SHARE * largest)
        )
        by_language.setdefault(language, set()).update(levels[depth])
    return max(by_language.values(), key=len, default=set())


def _page_links(page_url: str, html_text: str) -> list[str]:
    """Return the absolute URLs of the links on a page to the same website."""
    from selectolax.parser import HTMLParser

    site = _site_host(page_url)
    links = []
    for link in HTMLParser(html_text).css("a[href]"):
        href = (link.attributes.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        url = urljoin(page_url, href)
        if urlsplit(url).scheme in ("http", "https") and _site_host(url) == site:
            links.append(url)
    return links


def _page_number(url: str) -> int:
    """Return the page number a pagination link points to, or 0."""
    parts = urlsplit(url)
    numbers = [
        int(value)
        for key, value in parse_qsl(parts.query)
        if key.lower() in PAGE_PARAMETERS and value.isdigit()
    ]
    match = PAGE_PATH.search(parts.path)
    if match:
        numbers.append(int(match.group(1)))
    numbers = [number for number in numbers if number <= MAX_PAGE_NUMBER]
    return max(numbers, default=0)


def _count_from_pagination(page_url: str, html_text: str) -> tuple[int, float]:
    """Estimate the listings of a paginated listing page.

    The estimate is the highest page number in the pagination links times the
    number of listings linked from the page.

    Returns:
        (count, confidence)
    """
    links = _page_links(page_url, html_text)
    page_numbers = [_page_number(link) for link in links]
    last_page = max(page_numbers, default=0)
    listing_links = [link for link, number in zip(links, page_numbers, strict=True) if not number]
    per_page = len(_listing_detail_paths(listing_links))
    if last_page >= 2 and per_page >= 2:
        return last_page * per_page, 0.6
    return 0, 0.0


def _count_from_html(html_text: str, stated_totals: bool = True) -> tuple[int, float]:
    """Try to count properties from HTML listing page.

    Args:
        html_text: The HTML of the page.
        stated_totals: Read count phrases such as "454 biens". A homepage states
            other figures in the same form ("plus de 8000 logements gérés"), so
            only a listing page is read for them.

    Returns:
        (count, confidence) — confidence is 0.0-1.0.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html_text)

    # Get visible text (remove script/style content)
    for tag in tree.css("script, style"):
        tag.decompose()
    visible_text = tree.body.text(separator=" ") if tree.body else html_text

    # Count phrases in the visible text (high confidence). The text of adjacent
    # elements runs together, so a price can precede a heading: "€ 680.000 Biens".
    for pattern in LISTING_COUNT_PATTERNS if stated_totals else []:
        for match in pattern.finditer(visible_text):
            if CURRENCY.search(visible_text[max(0, match.start(1) - 5) : match.start(1)]):
                continue
            count = _parse_count(match.group(1))
            if count:
                return count, 0.7

    # Count listing item elements
    max_count = max(len(tree.css(selector)) for selector in LISTING_ITEM_SELECTORS)
    if max_count > 0:
        # This is a page count, not total — low confidence
        return max_count, 0.3

    # Check JSON data embedded in page (lower confidence); re-parse the original
    # HTML to get the script tags decomposed above
    for script in HTMLParser(html_text).css("script"):
        text = script.text()
        if not text:
            continue
        for pattern in JSON_COUNT_PATTERNS:
            json_match = pattern.search(text)
            if json_match:
                count = _parse_count(json_match.group(1))
                if count:
                    return count, 0.5

    return 0, 0.0


def _listing_page_candidates(base_url: str, html_text: str) -> list[str]:
    """Return the links of a homepage that may lead to a listing page.

    These are the links to the same website whose path contains a listing or
    search term, ordered by the number of path segments: an overview comes
    before a listing. Links that differ only in their query string are one
    candidate.
    """
    candidates: dict[str, tuple[int, str]] = {}
    for url in _page_links(base_url, html_text):
        _language, segments = _split_url(url)
        words = [word for segment in segments for word in _words(segment)]
        if any(word in LISTING_TERMS or word in SEARCH_TERMS for word in words):
            candidates.setdefault(url.split("?")[0].split("#")[0], (len(segments), url))
    return [url for _depth, url in sorted(candidates.values(), key=lambda entry: entry[0])]


def _find_listing_page_url(base_url: str, html_text: str) -> str | None:
    """Find a listing page URL from the homepage HTML."""
    candidates = _listing_page_candidates(base_url, html_text)
    return candidates[0] if candidates else None


async def _listing_page_estimates(
    homepage_url: str, html_text: str, client: httpx.AsyncClient
) -> list[tuple[int, float, str]]:
    """Return the estimates from the listing page linked from the homepage.

    Fetches the first ``MAX_LISTING_PAGES`` candidates and keeps the page that
    links to the most listings; a page about selling or renting out a property
    also matches the listing terms, but links to few listings or none.
    """
    best: tuple[int, list[tuple[int, float, str]]] | None = None
    for url in _listing_page_candidates(homepage_url, html_text)[:MAX_LISTING_PAGES]:
        try:
            resp = await client.get(url, timeout=settings.audit_http_timeout)
        except Exception as exc:
            logger.debug("Listing page fetch failed for %s: %s", url, exc)
            continue
        if resp.status_code >= 400:
            continue
        listings = len(_listing_detail_paths(_page_links(url, resp.text)))
        if best is None or listings > best[0]:
            best = (listings, _page_estimates(url, resp.text))
    return best[1] if best else []


async def _fetch_sitemap(sitemap_url: str, client: httpx.AsyncClient) -> str | None:
    """Fetch a sitemap document, decompressing a gzipped sitemap."""
    try:
        resp = await client.get(sitemap_url, timeout=settings.sitemap_timeout)
        if resp.status_code >= 400:
            return None
        content = resp.content
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        return content.decode(resp.encoding or "utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Sitemap fetch failed for %s: %s", sitemap_url, exc)
        return None


async def _sitemap_page_urls(sitemap_urls: list[str], client: httpx.AsyncClient) -> list[str]:
    """Return the page URLs of sitemaps, following sitemap indexes.

    Fetches at most ``MAX_SITEMAPS`` documents.
    """
    queue = list(sitemap_urls)
    fetched: set[str] = set()
    page_urls: list[str] = []
    while queue and len(fetched) < MAX_SITEMAPS:
        sitemap_url = queue.pop(0)
        if sitemap_url in fetched:
            continue
        fetched.add(sitemap_url)
        content = await _fetch_sitemap(sitemap_url, client)
        if content is None:
            continue
        locs = [html.unescape(loc) for loc in _LOC.findall(content)]
        if "<sitemapindex" in content:
            queue.extend(locs)
        else:
            page_urls.extend(locs)
    return page_urls


async def _count_from_sitemap(
    sitemap_urls: list[str],
    client: httpx.AsyncClient,
) -> tuple[int, float]:
    """Count the listings in the sitemaps of a website.

    Returns:
        (count, confidence) — the number of listing detail pages with confidence
        0.8, or the number of pages with confidence 0.4 when no page is a
        listing.
    """
    page_urls = await _sitemap_page_urls(sitemap_urls, client)
    listings = _listing_detail_paths(page_urls)
    if listings:
        return len(listings), 0.8
    pages = set(page_urls)
    if pages:
        return len(pages), 0.4
    return 0, 0.0


async def _cross_check_wp_rest(
    base_url: str,
    sitemap_count: int,
    client: httpx.AsyncClient,
) -> tuple[int, float] | None:
    """Cross-check sitemap count against WordPress REST API ``X-WP-Total``.

    Probes a short list of candidate listing endpoints under ``/wp-json/wp/v2/``
    with ``per_page=1``.  If any endpoint responds with an ``X-WP-Total`` header
    whose value is less than half the sitemap count, the authoritative API total
    is returned along with a reduced confidence score.  Any error (network,
    parse, status) skips the cross-check, leaving the sitemap result untouched.

    Returns:
        ``(api_count, 0.5)`` when a significant discrepancy is found,
        ``None`` when no reconcilable REST endpoint responded.
    """
    WP_REST_CANDIDATES: list[str] = [
        "/wp-json/wp/v2/estate_property",
        "/wp-json/wp/v2/property",
        "/wp-json/wp/v2/properties",
    ]
    parts = urlsplit(base_url)
    for candidate in WP_REST_CANDIDATES:
        url = f"{parts.scheme}://{parts.netloc}{candidate}?per_page=1"
        try:
            resp = await client.get(url, timeout=settings.sitemap_timeout)
            if resp.status_code == 200:
                raw = resp.headers.get("X-WP-Total")
                if raw is not None:
                    api_count = int(raw)
                    if api_count > 0 and sitemap_count > api_count * 2:
                        logger.debug(
                            "WP REST cross-check: sitemap=%d, %s=%d — "
                            "preferring authoritative API count",
                            sitemap_count,
                            candidate,
                            api_count,
                        )
                        return api_count, 0.5
                    # API total is within 2× for this endpoint — keep probing
                    # the next candidate (a later endpoint may reveal the
                    # discrepancy).
        except Exception:
            continue
    return None


def _page_estimates(
    page_url: str, html_text: str, stated_totals: bool = True
) -> list[tuple[int, float, str]]:
    """Return the estimates from a page: from its HTML and from its pagination."""
    return [
        (*_count_from_html(html_text, stated_totals), "listing_page"),
        (*_count_from_pagination(page_url, html_text), "listing_page"),
    ]


async def count_properties(
    base_url: str,
    homepage_response: httpx.Response | None = None,
    sitemap_urls: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> PropertyCountResult:
    """Attempt to count listed properties on a website.

    Collects an estimate from the homepage, a listing page and the sitemaps.
    Each source tends to count low: a listing page shows one category, a
    sitemap may leave out a section. The result is therefore the highest of the
    estimates with at least ``STRONG_CONFIDENCE``, or else the most confident
    of the partial counts.

    Args:
        base_url: Website URL.
        homepage_response: Optional pre-fetched homepage response.
        sitemap_urls: Optional list of sitemap URLs (from robots.txt).
        client: Optional httpx.AsyncClient.

    Returns:
        PropertyCountResult with count, source, and confidence.
    """
    result = PropertyCountResult()
    own_client = client is None and homepage_response is None
    if own_client:
        client = httpx.AsyncClient(timeout=settings.audit_http_timeout, follow_redirects=True)

    # (count, confidence, source)
    estimates: list[tuple[int, float, str]] = []
    try:
        if homepage_response is None:
            if client is None:
                raise ValueError("Need client or response")
            homepage_response = await client.get(base_url)
        try:
            # The URL after redirects, to resolve the relative links of the page
            homepage_url = str(homepage_response.url)
        except RuntimeError:
            homepage_url = base_url

        estimates.extend(_page_estimates(homepage_url, homepage_response.text, stated_totals=False))

        if client:
            estimates.extend(
                await _listing_page_estimates(homepage_url, homepage_response.text, client)
            )
            parts = urlsplit(base_url)
            sitemaps = sitemap_urls[:5] if sitemap_urls else []
            if not sitemaps:
                sitemaps = [f"{parts.scheme}://{parts.netloc}/sitemap.xml"]
            count, conf = await _count_from_sitemap(sitemaps, client)
            if count > 0:
                reconciled = await _cross_check_wp_rest(base_url, count, client)
                if reconciled is not None:
                    count, conf = reconciled
                estimates.append((count, conf, "sitemap"))

    except Exception as exc:
        logger.warning("Property count failed for %s: %s", base_url, exc)
    finally:
        if own_client and client:
            await client.aclose()

    estimates = [estimate for estimate in estimates if estimate[0] > 0]
    strong = [estimate for estimate in estimates if estimate[1] >= STRONG_CONFIDENCE]
    if strong:
        best = max(strong)
    elif estimates:
        best = max(estimates, key=lambda estimate: (estimate[1], estimate[0]))
    else:
        return result
    result.count, result.confidence, result.source = best
    return result
