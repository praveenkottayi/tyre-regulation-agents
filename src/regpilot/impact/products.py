"""
Product catalogue (CSV) -> one attribute dict per tyre size, using exactly the
attribute names the watcher's Criteria understand (core/models.py:TYRE_ATTRIBUTES).

    ranges.csv         range_id, brand, range_name, tyre_class, category_of_use,
                       severe_snow, ice_grip, traction, run_flat
    range_markets.csv  range_id, market
    sizes.csv          size_id, range_id, designation, section_width_mm, aspect_ratio,
                       rim, load_index, speed_symbol, extra_load, reinforced, c_cp_lt_marking
    test_results.csv   size_id, metric, value, unit, test_date

A blank cell stays None ("not known"), so it is never treated as False.
"""

import csv
from pathlib import Path

# Speed symbol -> maximum speed (km/h), per UN R30 / R54.
SPEED_SYMBOLS = {"J": 100, "K": 110, "L": 120, "M": 130, "N": 140, "P": 150, "Q": 160, "R": 170, "S": 180, "T": 190,
                 "U": 200, "H": 210, "V": 240, "W": 270, "Y": 300}

BOOL_FIELDS = ("severe_snow", "ice_grip", "traction", "run_flat", "extra_load", "reinforced", "c_cp_lt_marking")
NUM_FIELDS = ("section_width_mm", "aspect_ratio", "load_index")


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _bool(v: str) -> bool | None:
    v = (v or "").strip().lower()
    return {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(v)


def load_catalogue(folder: str | Path) -> list[dict]:
    folder = Path(folder)
    ranges = {r["range_id"]: r for r in _read(folder / "ranges.csv")}
    markets: dict[str, list[str]] = {}
    for m in _read(folder / "range_markets.csv"):
        markets.setdefault(m["range_id"], []).append(m["market"])
    tests: dict[str, list[dict]] = {}
    for t in _read(folder / "test_results.csv"):
        tests.setdefault(t["size_id"], []).append({**t, "value": float(t["value"])})

    tyres = []
    for s in _read(folder / "sizes.csv"):
        r = ranges[s["range_id"]]
        merged = {**r, **{k: v for k, v in s.items() if v != ""}}
        tyre = {
            "size_id": s["size_id"], "range_id": r["range_id"], "brand": r["brand"], "range_name": r["range_name"],
            "designation": s["designation"], "tyre_class": r["tyre_class"],
            "markets": markets.get(r["range_id"], []),
            "category_of_use": r["category_of_use"] or None,
            "speed_kmh": SPEED_SYMBOLS.get((s.get("speed_symbol") or "").upper()),
            "tests": sorted(tests.get(s["size_id"], []), key=lambda t: t["test_date"]),
        }
        for f in BOOL_FIELDS:
            tyre[f] = _bool(merged.get(f, ""))
        for f in NUM_FIELDS:
            tyre[f] = float(merged[f]) if merged.get(f) else None
        tyres.append(tyre)
    return tyres
