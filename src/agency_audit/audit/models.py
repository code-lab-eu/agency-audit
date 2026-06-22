"""Data models for audit results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RobotsResult:
    """Result of robots.txt fetch & parse."""

    fetched: bool = False
    allows_scraping: bool = True  # default allow if no robots.txt
    crawl_delay: float | None = None
    sitemap_urls: list[str] = field(default_factory=list)
    raw_content: str | None = None
    error: str | None = None


@dataclass
class AntiScrapingResult:
    """Result of anti-scraping detection."""

    detected: bool = False
    cloudflare: bool = False
    recaptcha: bool = False
    bot_detection_headers: bool = False
    js_only_rendering: bool = False
    details: list[str] = field(default_factory=list)


@dataclass
class ApiDetectionResult:
    """Result of API endpoint detection."""

    detected: bool = False
    api_type: str | None = None  # "graphql", "rest", "json-ld"
    api_url: str | None = None
    endpoints_found: list[str] = field(default_factory=list)


@dataclass
class PropertyCountResult:
    """Result of property count estimation."""

    count: int = 0
    source: str = "unknown"  # "listing_page", "sitemap", "api", "json-ld"
    confidence: float = 0.0


@dataclass
class ListingQualityResult:
    """Result of listing quality checks."""

    has_structured_data: bool = False
    has_images: bool = False
    has_descriptions: bool = False
    has_prices: bool = False
    has_locations: bool = False
    has_property_map: bool = False
    quality_score: float = 0.0  # 0.0 - 1.0


@dataclass
class TechStackResult:
    """Result of technology stack detection."""

    framework: str | None = None
    hosting: str | None = None
    cdn: str | None = None
    technologies: list[str] = field(default_factory=list)


@dataclass
class AuditData:
    """Complete audit result combining all checks."""

    url: str = ""
    robots: RobotsResult = field(default_factory=RobotsResult)
    anti_scraping: AntiScrapingResult = field(default_factory=AntiScrapingResult)
    api_detection: ApiDetectionResult = field(default_factory=ApiDetectionResult)
    property_count: PropertyCountResult = field(default_factory=PropertyCountResult)
    listing_quality: ListingQualityResult = field(default_factory=ListingQualityResult)
    tech_stack: TechStackResult = field(default_factory=TechStackResult)
    response_time_ms: float | None = None
    ssl_valid: bool = True
    language: str | None = None
    notes: str = ""
    score: int = 0
    score_breakdown: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to dict for JSONB storage."""
        return {
            "url": self.url,
            "robots_txt_allows": self.robots.allows_scraping,
            "robots_txt_fetched": self.robots.fetched,
            "robots_crawl_delay": self.robots.crawl_delay,
            "robots_sitemap_urls": self.robots.sitemap_urls,
            "has_anti_scraping": self.anti_scraping.detected,
            "anti_scraping_details": self.anti_scraping.details,
            "cloudflare": self.anti_scraping.cloudflare,
            "recaptcha": self.anti_scraping.recaptcha,
            "has_api": self.api_detection.detected,
            "api_type": self.api_detection.api_type,
            "api_url": self.api_detection.api_url,
            "api_endpoints": self.api_detection.endpoints_found,
            "property_count": self.property_count.count,
            "property_count_source": self.property_count.source,
            "property_count_confidence": self.property_count.confidence,
            "has_structured_data": self.listing_quality.has_structured_data,
            "listings_have_images": self.listing_quality.has_images,
            "listings_have_descriptions": self.listing_quality.has_descriptions,
            "listings_have_prices": self.listing_quality.has_prices,
            "listings_have_locations": self.listing_quality.has_locations,
            "has_property_map": self.listing_quality.has_property_map,
            "listing_quality_score": self.listing_quality.quality_score,
            "technology_stack": self.tech_stack.technologies,
            "framework": self.tech_stack.framework,
            "hosting": self.tech_stack.hosting,
            "cdn": self.tech_stack.cdn,
            "response_time_ms": self.response_time_ms,
            "ssl_valid": self.ssl_valid,
            "language": self.language,
            "notes": self.notes,
            "score": self.score,
            "score_breakdown": self.score_breakdown,
        }
