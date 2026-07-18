"""Derived-field rules, kept in one place so they are easy to audit and change.

These are inferences, not upstream facts. The database records that they were
derived (naics_source='osm_crosswalk') precisely so a consumer can tell the
difference between "the business declared this" and "we concluded this".
"""
from __future__ import annotations

# Keys that classify a business, in priority order. The first one present wins
# and becomes primary_tag_key/primary_tag_val.
CLASSIFY_KEYS = ("shop", "amenity", "craft", "office", "healthcare",
                 "tourism", "leisure")

# Storefront rule
# --------------
# The premise: OSM maps what is physically visible on the ground. A `shop` or a
# `craft` workshop is a walk-in premises essentially by definition. Professional
# `office` entries are not - a lawyer's office is a physical place but not a
# storefront in the retail sense, and many are home-based.
STOREFRONT_ALWAYS = {"shop", "craft"}
STOREFRONT_NEVER = {"office"}
STOREFRONT_BY_VALUE = {
    "amenity": {"restaurant", "cafe", "bar", "pub", "fast_food", "ice_cream",
                "bank", "pharmacy", "fuel", "veterinary", "car_wash",
                "car_rental", "cinema", "theatre", "library", "post_office"},
    "tourism": {"hotel", "motel", "guest_house", "museum", "gallery"},
    "leisure": {"fitness_centre", "sports_centre", "golf_course"},
    "healthcare": None,  # None => any value counts as a storefront
}

RESTAURANT_VALUES = {"restaurant", "cafe", "bar", "pub", "fast_food",
                     "ice_cream", "food_court"}


def primary_tag(tags: dict) -> tuple[str | None, str | None]:
    for key in CLASSIFY_KEYS:
        if key in tags:
            return key, tags[key]
    return None, None


def has_storefront(key: str | None, val: str | None) -> int | None:
    if key is None:
        return None
    if key in STOREFRONT_NEVER:
        return 0
    if key in STOREFRONT_ALWAYS:
        return 1
    if key in STOREFRONT_BY_VALUE:
        allowed = STOREFRONT_BY_VALUE[key]
        return 1 if allowed is None or val in allowed else 0
    return None


def is_restaurant(key: str | None, val: str | None) -> int:
    return int(key == "amenity" and val in RESTAURANT_VALUES)


def first(tags: dict, *keys: str) -> str | None:
    """OSM records the same fact under several keys; take whichever is present."""
    for k in keys:
        if tags.get(k):
            return tags[k]
    return None
