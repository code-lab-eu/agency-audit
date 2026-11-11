"""Tests for the website URL key and the tracking-parameter cleaner."""

from __future__ import annotations

import pytest

from agency_audit.urls import clean_website_url, is_maps_only_url, website_key

HEYLEN = (
    "https://www.heylenvastgoed.be/?utm_source=organic&utm_medium=google+my+business"
    "&utm_campaign=gmb+antwerpen",
    "https://www.heylenvastgoed.be/?utm_source=organic&utm_medium=google+my+business"
    "&utm_campaign=gmb+antwerpen+%27t+zand",
    "https://www.heylenvastgoed.be/?utm_source=organic&utm_medium=google+my+business"
    "&utm_campaign=gmb+hasselt&utm_id=google",
)


def test_branch_tracking_links_give_one_key() -> None:
    assert {website_key(url) for url in HEYLEN} == {"heylenvastgoed.be"}


@pytest.mark.parametrize(
    "variant",
    [
        "https://www.heylenvastgoed.be/",
        "http://www.heylenvastgoed.be",
        "https://heylenvastgoed.be",
        "HTTPS://WWW.HeylenVastgoed.BE/",
        "https://www.heylenvastgoed.be/#contact",
        "https://www.heylenvastgoed.be:443/",
        "http://www.heylenvastgoed.be:80",
        "https://www.heylenvastgoed.be/?gclid=abc&fbclid=def",
        "  https://www.heylenvastgoed.be/  ",
    ],
)
def test_spelling_variants_give_one_key(variant: str) -> None:
    assert website_key(variant) == "heylenvastgoed.be"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "https://www.engelvoelkers.com/be/nl/shops/antwerpen-centrum",
            "https://www.engelvoelkers.com",
        ),
        (
            "https://www.engelvoelkers.com/be/nl/shops/antwerpen-centrum",
            "https://www.engelvoelkers.com/be/en/shops/brussels-centre",
        ),
        ("https://www.era.be/fr/era-chatelain", "http://www.era.be/stadsgoed"),
        ("https://maps.google.com/?cid=A", "https://maps.google.com/?cid=B"),
        ("https://example.com:8080/", "https://example.com/"),
    ],
)
def test_different_websites_on_one_domain_keep_different_keys(first: str, second: str) -> None:
    assert website_key(first) != website_key(second)


def test_key_keeps_path_case_and_other_parameters_sorted() -> None:
    assert website_key("https://www.era.be/fr/ERA-Chatelain/?utm_campaign=x&b=2&a=1") == (
        "era.be/fr/ERA-Chatelain?a=1&b=2"
    )
    assert website_key("https://x.be/?b=2&a=1") == website_key("https://x.be/?a=1&b=2")


def test_key_does_not_decode_query_segments() -> None:
    assert website_key("https://x.be/?q=a%20b") == "x.be?q=a%20b"


def test_key_survives_an_invalid_port() -> None:
    assert website_key("https://www.x.be:abc/") == "x.be:abc"


def test_clean_url_removes_only_tracking_and_fragment() -> None:
    assert clean_website_url(HEYLEN[2]) == "https://www.heylenvastgoed.be/"
    assert (
        clean_website_url("http://www.era.be/fr/era-chatelain?utm_campaign=x&lang=fr#top")
        == "http://www.era.be/fr/era-chatelain?lang=fr"
    )
    assert clean_website_url("https://maps.google.com/?cid=123") == (
        "https://maps.google.com/?cid=123"
    )


def test_is_maps_only_url() -> None:
    assert is_maps_only_url("https://maps.google.com/?cid=ChIJoR3C94XDw0cRxbuBuURXuwo")
    assert not is_maps_only_url("https://www.heylenvastgoed.be/")
    assert not is_maps_only_url("https://maps.google.com/maps/place/x")
