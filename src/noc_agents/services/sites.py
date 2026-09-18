"""Site catalogue lookup — reads ``data/seed/safaricom_sites.json`` once and caches it.

DATA PROVENANCE (read this before trusting a coordinate)
--------------------------------------------------------
The catalogue is a *demo* catalogue. The seven original fields come from the
project seed; the geo / power fields were backfilled from public sources and are
deliberately coarse:

* ``lat`` / ``lon`` are **published town, suburb or county-HQ centroids rounded to
  2 decimal places (~1.1 km)** — they are NOT surveyed site positions. The 2-dp
  rounding is the honesty signal: never present these as a site's real location,
  and never compute a distance or an access route from them. Per-record
  ``geo_precision`` says what the coordinate actually is (see
  ``GEO_PRECISION_LEGEND``). Real per-site GPS is a NOC-floor procurement item.
* ``ward`` is ``None`` for every site: the seed carries no addresses and the site
  names resolve only to a constituency or a locality (e.g. "Kayole" spans Kayole
  North / Central / South), so no ward could be established. Left null rather
  than invented.
* ``kplc_region`` uses Kenya Power's **published region names** (North Rift,
  Central Rift, Mt. Kenya, South Nyanza, Nairobi, Coast, North Eastern, Western —
  kplc.co.ke/regional-managers). The *county -> region* mapping is inferred from
  secondary KPLC listings, not from a KPLC source document; per-record
  ``kplc_source`` records which. ``None`` means the mapping could not be
  established (Kiambu/Thika).
* ``kplc_area_hints`` are **real locality names likely to appear in a KPLC
  planned-interruption notice covering this area** — matching hints, not KPLC
  feeder or transformer identifiers.
* ``riverine`` is a conservative default: ``False`` means "no documented river /
  lake flood-plain exposure at locality level", NOT "surveyed as safe". Only
  Kisumu is flagged ``True`` (Winam Gulf / Kano-Nyando flood plain). A proper
  flood-plain overlay is still outstanding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
SEED_PATH = ROOT / "data" / "seed" / "safaricom_sites.json"

#: The site_class vocabulary fixed by the spec (§7.0.7).
SITE_CLASSES: tuple[str, ...] = ("MACRO", "HUB", "CORE", "SMALL_CELL", "FTTH_POP")

#: What a ``geo_precision`` value means. Every value is ~1 km or coarser.
GEO_PRECISION_LEGEND: dict[str, str] = {
    "town_centroid_2dp": "published centroid of the named town, rounded to 2 dp",
    "suburb_centroid_2dp": "published centroid of the named suburb/estate, rounded to 2 dp",
    "county_hq_town_centroid_2dp": (
        "site is rural or its locality is unknown; coordinate is the county HQ town centroid"
    ),
    "area_approx_2dp": "only a sub-city area is known; coordinate is that area's approximate centre",
    "demo_placeholder_city_centroid_2dp": (
        "synthetic demo node with no real location; coordinate is the city centroid"
    ),
}

#: One-line banner for UI/report surfaces that show catalogue coordinates.
GEO_DISCLAIMER = (
    "Coordinates are town/suburb/county-centroid approximations rounded to 2 dp "
    "(~1.1 km), not surveyed site positions — pending the NOC floor's site register."
)

_FIELDS: tuple[str, ...] = (
    "site_id",
    "site_name",
    "site_type",
    "region_code",
    "county",
    "radio_oem",
    "coverage_note",
    "ward",
    "lat",
    "lon",
    "parent_hub_id",
    "site_class",
    "riverine",
    "kplc_region",
    "kplc_area_hints",
)


@dataclass(frozen=True, slots=True)
class SiteRecord:
    """One catalogue row. The 15 spec fields plus the two provenance markers."""

    site_id: str
    site_name: str
    site_type: str
    region_code: str
    county: str | None = None
    radio_oem: str = "MIXED"
    coverage_note: str | None = None
    # --- backfilled (§7.0.7) ---
    ward: str | None = None
    lat: float | None = None
    lon: float | None = None
    parent_hub_id: str | None = None
    site_class: str | None = None
    riverine: bool = False
    kplc_region: str | None = None
    kplc_area_hints: tuple[str, ...] = ()
    # --- provenance markers (see module docstring) ---
    geo_precision: str | None = None
    kplc_source: str | None = None

    @property
    def is_hub(self) -> bool:
        return self.site_class == "HUB"

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lon is not None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SiteRecord":
        hints = raw.get("kplc_area_hints") or ()
        lat, lon = raw.get("lat"), raw.get("lon")
        return cls(
            site_id=str(raw["site_id"]),
            site_name=str(raw.get("site_name") or raw["site_id"]),
            site_type=str(raw.get("site_type") or ""),
            region_code=str(raw.get("region_code") or ""),
            county=raw.get("county"),
            radio_oem=str(raw.get("radio_oem") or "MIXED"),
            coverage_note=raw.get("coverage_note"),
            ward=raw.get("ward"),
            lat=None if lat is None else float(lat),
            lon=None if lon is None else float(lon),
            parent_hub_id=raw.get("parent_hub_id"),
            site_class=raw.get("site_class"),
            riverine=bool(raw.get("riverine", False)),
            kplc_region=raw.get("kplc_region"),
            kplc_area_hints=tuple(str(h) for h in hints),
            geo_precision=raw.get("geo_precision"),
            kplc_source=raw.get("kplc_source"),
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe mapping, field order matching the seed file."""
        out: dict[str, Any] = {}
        for name in _FIELDS:
            value = getattr(self, name)
            out[name] = list(value) if name == "kplc_area_hints" else value
        out["geo_precision"] = self.geo_precision
        out["kplc_source"] = self.kplc_source
        return out


_CACHE: dict[str, SiteRecord] | None = None


def _key(site_id: str) -> str:
    return str(site_id).strip().upper()


def _load() -> dict[str, SiteRecord]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    rows: list[dict[str, Any]] = []
    if SEED_PATH.exists():
        parsed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        if isinstance(parsed, list):
            rows = [r for r in parsed if isinstance(r, dict) and r.get("site_id")]
    cache: dict[str, SiteRecord] = {}
    for raw in rows:
        rec = SiteRecord.from_dict(raw)
        cache[_key(rec.site_id)] = rec
    _CACHE = cache
    return cache


def reload_sites() -> int:
    """Drop the cache and re-read the seed. Returns the number of sites loaded."""
    global _CACHE
    _CACHE = None
    return len(_load())


def lookup_site(site_id: str | None) -> SiteRecord | None:
    """Return the catalogue row for ``site_id``, or ``None`` if it is not catalogued."""
    if not site_id:
        return None
    return _load().get(_key(site_id))


def all_sites() -> tuple[SiteRecord, ...]:
    """Every catalogued site, in seed order."""
    return tuple(_load().values())


def sites_by_region(region_code: str) -> tuple[SiteRecord, ...]:
    want = str(region_code or "").strip().upper()
    return tuple(s for s in all_sites() if s.region_code.upper() == want)


def parent_hub(site_id: str | None) -> SiteRecord | None:
    """The parent HUB record of ``site_id``, if the site has one and it resolves."""
    site = lookup_site(site_id)
    if site is None or not site.parent_hub_id:
        return None
    return lookup_site(site.parent_hub_id)


def child_sites(hub_id: str | None) -> tuple[SiteRecord, ...]:
    """Every catalogued site whose ``parent_hub_id`` points at ``hub_id``."""
    if not hub_id:
        return ()
    want = _key(hub_id)
    return tuple(s for s in all_sites() if s.parent_hub_id and _key(s.parent_hub_id) == want)


__all__ = [
    "GEO_DISCLAIMER",
    "GEO_PRECISION_LEGEND",
    "SEED_PATH",
    "SITE_CLASSES",
    "SiteRecord",
    "all_sites",
    "child_sites",
    "lookup_site",
    "parent_hub",
    "reload_sites",
    "sites_by_region",
]
