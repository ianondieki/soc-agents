"""Which town or region a complaint names, read against the operator profile's own regions.

``link_incident`` has to turn "hakuna network huku Kayole" into "the open Embakasi HUB
incident". Two facts make that possible without a second, hand-kept list of towns:

* the operator profile already describes every region in words the floor uses
  (``coverage_areas: ["Eastlands (Umoja, Kayole, Pipeline, Donholm)", ...]``, ``counties``,
  ``label``), so a gazetteer is *derived* from it -- a new profile brings its own places;
* an open incident names its site ("Nakuru Rift HUB") and county, so a town that is not in
  the profile at all still matches the incident directly (:mod:`tools` does that half).

Parsing the coverage strings is deliberately plain: split on ``/ ( ) ,``, and from each piece
strip trailing qualifiers ("fringe east", "lakeside metro", "Island") so "Ruiru / Juja fringe
east" yields Ruiru and Juja. A handful of pieces are ordinary English words that would fire
on unrelated complaints ("Port" -- as in *port my number*), and those are dropped by name.
A place can belong to more than one region (Kiambu is in three); it keeps all of them and
the incident match decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from noc_agents.support.text import normalise, phrase_pattern

#: Trailing words that qualify a place rather than name it.
_QUALIFIERS: frozenset[str] = frozenset(
    {"east", "west", "north", "south", "fringe", "corridor", "metro", "lakeside", "highlands",
     "island", "area", "road", "rural", "urban", "town", "cbd"}
)
#: Pieces that are everyday words first and places second; matching them would misfire.
_NOT_PLACES: frozenset[str] = frozenset({"port", "industrial", "upper", "lower", "central", "metro"})
_SPLIT = re.compile(r"[/(),;]")


@dataclass(frozen=True)
class PlaceMention:
    name: str  # normalised, as matched in the complaint
    regions: tuple[str, ...]


def _strip_qualifiers(piece: str) -> str:
    words = normalise(piece).split()
    while len(words) > 1 and words[-1] in _QUALIFIERS:
        words.pop()
    return " ".join(words)


def _names(text: str) -> Iterable[str]:
    for piece in _SPLIT.split(text or ""):
        full = normalise(piece)
        if full and full not in _NOT_PLACES:
            yield full
        short = _strip_qualifiers(piece)
        if short and short != full and short not in _NOT_PLACES:
            yield short


class Gazetteer:
    """Place name -> regions, with a longest-match-first finder over complaint text."""

    def __init__(self, places: dict[str, set[str]]) -> None:
        self._regions = {name: tuple(sorted(regions)) for name, regions in places.items() if len(name) >= 3}
        # Longest first, so "nairobi east" wins over "nairobi" in the same span.
        ordered = sorted(self._regions, key=lambda n: (-len(n), n))
        self._patterns = [(name, phrase_pattern(name)) for name in ordered]

    @classmethod
    def from_regions(cls, regions: dict[str, Any]) -> "Gazetteer":
        """Build from ``OperatorConfig.regions`` (``RegionConfig`` objects or plain dicts)."""
        places: dict[str, set[str]] = {}
        for code, region in regions.items():
            data = region if isinstance(region, dict) else region.model_dump()
            texts = [data.get("label", ""), *data.get("coverage_areas", []), *data.get("counties", [])]
            for text in texts:
                for name in _names(text):
                    places.setdefault(name, set()).add(code)
        return cls(places)

    def regions_of(self, name: str) -> tuple[str, ...]:
        return self._regions.get(normalise(name), ())

    def find(self, text: str) -> list[PlaceMention]:
        """Every place the text names, longest match first, never two over the same words."""
        norm = normalise(text)
        taken: list[tuple[int, int]] = []
        found: list[tuple[int, PlaceMention]] = []
        for name, pattern in self._patterns:
            for match in pattern.finditer(norm):
                span = match.span()
                if any(span[0] < end and start < span[1] for start, end in taken):
                    continue
                taken.append(span)
                found.append((span[0], PlaceMention(name=name, regions=self._regions[name])))
        return [mention for _, mention in sorted(found, key=lambda item: item[0])]
