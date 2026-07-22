"""
Station Lookup — map Polymarket weather markets to their resolution station.

CRITICAL: Polymarket weather markets resolve against a NAMED AIRPORT STATION
via Wunderground history, NOT a city centroid. The station name appears in
the market description text (e.g. "LaGuardia Airport Station" → KLGA).

This module parses the description to extract the ICAO code, then returns
the station's coordinates and metadata for forecast queries.
"""

import re
import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Station:
    icao: str
    name: str
    city: str
    country: str
    lat: float
    lon: float
    elevation_m: float
    units: str  # "F" or "C"
    wunderground_path: str


class StationLookup:
    """Resolve a Polymarket market description to a Station object."""

    def __init__(self, stations_path: str = "config/stations.yaml"):
        path = Path(stations_path)
        if not path.exists():
            raise FileNotFoundError(f"Stations config not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f)
        self._stations = {
            icao: Station(icao=icao, **info)
            for icao, info in data.get("stations", {}).items()
        }
        self._keywords = data.get("description_keywords", [])

    def get_station(self, icao: str) -> Station:
        """Return Station by ICAO code. Raises KeyError if unknown."""
        if icao not in self._stations:
            raise KeyError(f"Unknown ICAO code: {icao}")
        return self._stations[icao]

    def parse_from_description(self, description: str) -> Optional[Station]:
        """
        Parse the market description text to find the resolution station.

        Polymarket descriptions look like:
          "This market will resolve to the temperature range that contains
           the highest temperature recorded at the LaGuardia Airport Station
           in degrees Fahrenheit on 16 Apr '26."

        We search for keyword matches (case-sensitive for ICAO codes,
        case-insensitive for names) and return the first match.
        Returns None if no station is found — the caller MUST raise,
        never guess a city centroid.
        """
        if not description:
            return None

        for entry in self._keywords:
            keyword = entry["keyword"]
            icao = entry["icao"]
            # ICAO codes are uppercase 4-char — match exact
            # Station names — match case-insensitive
            if len(keyword) <= 4 and keyword.isupper():
                pattern = r'\b' + re.escape(keyword) + r'\b'
            else:
                pattern = r'\b' + re.escape(keyword) + r'\b'
                # Also try case-insensitive for names
                if re.search(pattern, description, re.IGNORECASE):
                    return self._stations.get(icao)
                continue
            if re.search(pattern, description):
                return self._stations.get(icao)
        return None

    def all_stations(self) -> dict:
        """Return all known stations."""
        return dict(self._stations)


# --- Convenience functions for testing ---

def lookup(description: str, stations_path: str = "config/stations.yaml") -> Optional[Station]:
    """One-shot: parse description → Station. Returns None if not found."""
    return StationLookup(stations_path).parse_from_description(description)


if __name__ == "__main__":
    # Self-test with real Polymarket descriptions
    lookup_obj = StationLookup()

    test_cases = [
        (
            "This market will resolve to the temperature range that contains "
            "the highest temperature recorded at the LaGuardia Airport Station "
            "in degrees Fahrenheit on 16 Apr '26. The resolution source for "
            "this market will be information from Wunderground, specifically "
            "the highest temperature recorded for all times on this day by "
            "the Forecast for the LaGuardia Airport Station once information "
            "is finalized, available here: https://www.wunderground.com/history/"
            "daily/us/ny/new-york-city/KLGA.",
            "KLGA"
        ),
        (
            "This market will resolve to the temperature range that contains "
            "the highest temperature recorded at the Incheon Intl Airport Station "
            "in degrees Celsius on 17 Apr '26. The resolution source for this "
            "market will be information from Wunderground, specifically the "
            "highest temperature recorded for all times on this day by the "
            "Forecast for the Incheon Intl Airport Station once information is "
            "finalized, available here: https://www.wunderground.com/history/"
            "daily/kr/incheon/RKSI.",
            "RKSI"
        ),
        (
            "Some random market with no station mentioned.",
            None
        ),
    ]

    print("Station Lookup Self-Test")
    print("=" * 50)
    all_pass = True
    for desc, expected in test_cases:
        result = lookup_obj.parse_from_description(desc)
        got = result.icao if result else None
        status = "PASS" if got == expected else "FAIL"
        if got != expected:
            all_pass = False
        print(f"  [{status}] expected={expected}  got={got}")

    print()
    print(f"All tests passed: {all_pass}")