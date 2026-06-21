"""Technology stack detection module.

Identifies:
  - Framework (WordPress, Drupal, React, Next.js, Vue, Angular, custom, etc.)
  - Hosting (from Server header, IP-based heuristics)
  - CDN (from headers: Cloudflare, CloudFront, Fastly, etc.)
  - Additional technologies (jQuery, Bootstrap, etc.)

Uses httpx response headers + selectolax HTML parsing.
"""

from __future__ import annotations

import logging
import re

import httpx

from agency_audit.audit.models import TechStackResult

logger = logging.getLogger(__name__)

# Framework detection from headers
HEADER_FRAMEWORK_MAP = {
    "x-powered-by": {
        "express": "Express",
        "next": "Next.js",
        "nuxt": "Nuxt.js",
        "asp.net": "ASP.NET",
        "laravel": "Laravel",
        "django": "Django",
        "flask": "Flask",
    },
    "server": {
        "apache": "Apache",
        "nginx": "Nginx",
        "iis": "Microsoft IIS",
        "litespeed": "LiteSpeed",
    },
}

# Framework detection from HTML
HTML_FRAMEWORK_PATTERNS = [
    (r"wp-content|wp-includes|wp-json", "WordPress"),
    (r"wp-content/plugins/elementor", "WordPress + Elementor"),
    (r"drupal\.js|drupal\.org|sites/all/themes", "Drupal"),
    (r"joomla", "Joomla"),
    (r"__next_data__|_next/static", "Next.js"),
    (r"__nuxt__|_nuxt/", "Nuxt.js"),
    (r"react\.js|react-dom|data-reactroot", "React"),
    (r"vue\.js|vue\.min\.js|data-v-[a-f0-9]", "Vue.js"),
    (r"angular\.js|ng-app|ng-controller", "Angular"),
    (r"svelte", "Svelte"),
    (r"gatsby", "Gatsby"),
    (r'content="[^"]*shopify', "Shopify"),
    (r"wix\.com|wixstatic", "Wix"),
    (r"squarespace", "Squarespace"),
    (r"cdn-cgi/challenge-platform", "Cloudflare"),
]

# CDN detection from headers
CDN_HEADERS = {
    "cf-ray": "Cloudflare",
    "x-amz-cf-id": "CloudFront (AWS)",
    "x-fastly-request-id": "Fastly",
    "x-sucuri-id": "Sucuri",
    "x-cdn": None,  # value is the CDN name
    "x-edge": None,
    "x-cdn-origin-rtt": None,
    "x-akamai-transformed": "Akamai",
    "x-bolt-cdn": "Bolt",
    "x-vercel-id": "Vercel",
}

# Technology detection from HTML
TECH_PATTERNS = [
    (r"jquery", "jQuery"),
    (r"bootstrap", "Bootstrap"),
    (r"tailwind", "Tailwind CSS"),
    (r"font-awesome|fontawesome", "Font Awesome"),
    (r"google-analytics|gtag\(|googletagmanager", "Google Analytics"),
    (r"gtm\.js", "Google Tag Manager"),
    (r"adsbygoogle", "Google AdSense"),
    (r"fbq\(|facebook\.net", "Facebook Pixel"),
    (r"hotjar", "Hotjar"),
    (r"clarity\.ms", "Microsoft Clarity"),
    (r"recaptcha", "reCAPTCHA"),
    (r"leaflet", "Leaflet"),
    (r"mapbox", "Mapbox"),
    (r"swiper", "Swiper"),
    (r"slick-slider|slick\.js", "Slick Slider"),
    (r"owl-carousel", "Owl Carousel"),
    (r"google-maps|maps\.googleapis\.com", "Google Maps"),
    (r"fontawesome|font-awesome", "Font Awesome"),
    (r"elementor", "Elementor"),
    (r"wpbakery|js_composer", "WPBakery"),
    (r"contact-form-7|wpcf7", "Contact Form 7"),
    (r"yoast", "Yoast SEO"),
    (r"rankmath|rank-math", "Rank Math SEO"),
]

# Hosting detection from server header and patterns
HOSTING_PATTERNS = [
    (r"wp\s*engine", "WP Engine"),
    (r"kinsta", "Kinsta"),
    (r"siteground", "SiteGround"),
    (r"bluehost", "Bluehost"),
    (r"godaddy", "GoDaddy"),
    (r"hostinger", "Hostinger"),
    (r"contabo", "Contabo"),
    (r"hetzner", "Hetzner"),
    (r"ovh", "OVH"),
    (r"digiocean|digitalocean", "DigitalOcean"),
    (r"aws|amazonaws|cloudfront", "AWS"),
    (r"azure|windows\.net", "Azure"),
    (r"gcp|googleusercontent|googlecloud", "Google Cloud"),
    (r"vercel", "Vercel"),
    (r"netlify", "Netlify"),
    (r"heroku", "Heroku"),
]


def _detect_framework_from_headers(headers: httpx.Headers) -> str | None:
    """Detect framework from HTTP headers."""
    for header_name, mapping in HEADER_FRAMEWORK_MAP.items():
        value = headers.get(header_name, "").lower()
        for pattern, name in mapping.items():
            if pattern in value:
                return name
    return None


def _detect_framework_from_html(html_text: str) -> str | None:
    """Detect framework from HTML content."""
    html_lower = html_text.lower()
    for pattern, name in HTML_FRAMEWORK_PATTERNS:
        if re.search(pattern, html_lower):
            return name
    return None


def _detect_cdn(headers: httpx.Headers) -> str | None:
    """Detect CDN from HTTP headers."""
    lower_headers = {k.lower(): v for k, v in headers.items()}

    for header_name, cdn_name in CDN_HEADERS.items():
        if header_name in lower_headers:
            if cdn_name:
                return cdn_name
            # Return the header value itself (e.g. x-cdn: cloudfront)
            return lower_headers[header_name]

    return None


def _detect_hosting(headers: httpx.Headers, html_text: str) -> str | None:
    """Detect hosting provider from headers and HTML."""
    combined = " ".join(
        [
            headers.get("server", ""),
            headers.get("x-powered-by", ""),
            html_text[:5000].lower(),
        ]
    ).lower()

    for pattern, name in HOSTING_PATTERNS:
        if re.search(pattern, combined):
            return name

    return None


def _detect_technologies(html_text: str) -> list[str]:
    """Detect additional technologies from HTML."""
    html_lower = html_text.lower()
    techs = set()
    for pattern, name in TECH_PATTERNS:
        if re.search(pattern, html_lower):
            techs.add(name)
    return sorted(techs)


async def detect_tech_stack(
    url: str,
    response: httpx.Response | None = None,
    client: httpx.AsyncClient | None = None,
) -> TechStackResult:
    """Detect technology stack of a website.

    Args:
        url: Website URL.
        response: Optional pre-fetched response.
        client: Optional httpx.AsyncClient.

    Returns:
        TechStackResult with detected framework, hosting, CDN, technologies.
    """
    result = TechStackResult()
    own_client = client is None and response is None
    if own_client:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)

    try:
        if response is None:
            if client is None:
                raise ValueError("Either response or client must be provided")
            response = await client.get(url)

        html_text = response.text
        headers = response.headers

        # Detect framework (HTML is more specific — detects app frameworks like
        # WordPress/React; headers detect servers like Nginx/Apache)
        result.framework = _detect_framework_from_html(html_text)
        if result.framework is None:
            result.framework = _detect_framework_from_headers(headers)
        # Also capture the web server/header-level framework as hosting/tech
        header_framework = _detect_framework_from_headers(headers)
        if header_framework and header_framework != result.framework:
            if result.hosting is None and header_framework in {"Apache", "Nginx", "Microsoft IIS", "LiteSpeed"}:
                result.hosting = header_framework

        # Detect CDN
        result.cdn = _detect_cdn(headers)

        # Detect hosting
        result.hosting = _detect_hosting(headers, html_text)

        # Detect additional technologies
        result.technologies = _detect_technologies(html_text)

        # Add framework to technologies list
        if result.framework and result.framework not in result.technologies:
            result.technologies.insert(0, result.framework)

    except Exception as exc:
        logger.warning("Tech stack detection failed for %s: %s", url, exc)
    finally:
        if own_client and client:
            await client.aclose()

    return result
