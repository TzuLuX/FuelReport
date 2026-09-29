"""Aggregazione dei CSV giornalieri in statistiche compatte per provincia e comune.

Per ogni giorno dell'archivio vengono calcolati, per ciascuna combinazione
(categoria carburante, self/servito) e per ciascuna area:

* numero di impianti rilevati
* prezzo medio, minimo e massimo

Le aree considerate sono **provincia** e **comune**: regione e livello nazionale vengono
poi ottenuti per aggregazione (le partizioni sono disgiunte, quindi media/min/max sono
esatti). Il risultato di ogni giornata è salvato in un file compresso, così le
rielaborazioni successive del report sono immediate.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import IO, Iterator

from . import config, parsing, util
from .parsing import GeoIndex

#: Da incrementare quando cambia la logica di aggregazione (invalida la cache giornaliera).
CACHE_VERSION = 3

NFUEL = len(config.FUEL_ORDER)
FUEL_INDEX = {name: i for i, name in enumerate(config.FUEL_ORDER)}

_SELF_TRUE = {"1", "si", "s", "true", "self", "x"}
_SELF_FALSE = {"0", "no", "n", "false", "servito", ""}


def combo_key(fuel: str, mode: int) -> str:
    """Chiave compatta di una serie: ``"Benzina|0"`` (0 = servito, 1 = self)."""
    return f"{fuel}|{mode}"


def parse_combo(key: str) -> tuple[str, int]:
    fuel, _, mode = key.rpartition("|")
    return fuel, int(mode)


def _classify(label: str, cache: dict[str, str]) -> str:
    group = cache.get(label)
    if group is None:
        group = config.classify_fuel(label)
        cache[label] = group
    return group


def _self_flag(value: str) -> int | None:
    text = value.strip().lower()
    if text in _SELF_TRUE:
        return 1
    if text in _SELF_FALSE:
        return 0
    return None


def aggregate_day(
    day: date,
    fh: IO[bytes],
    geo: GeoIndex,
    fuel_cache: dict[str, str] | None = None,
    geo_key: str | None = None,
) -> dict[str, object]:
    """Aggrega un singolo file giornaliero (province e comuni) in un payload compatto."""
    fuel_cache = fuel_cache if fuel_cache is not None else {}
    try:
        delim, idx = parsing.price_columns(fh)
    except ValueError:
        return {"date": day.isoformat(), "rows": 0, "unmapped": 0, "prov": {}, "com": {}, "error": "header"}

    i_id = idx.get("id", 0)
    i_fuel = idx.get("carburante", 1)
    i_price = idx.get("prezzo", 2)
    i_self = idx.get("self", 3)
    fast = (i_id, i_fuel, i_price, i_self) == (0, 1, 2, 3)

    prov_acc: dict[int, list[float]] = {}
    com_acc: dict[int, list[float]] = {}
    mapping = geo.mapping
    rows = 0
    unmapped = 0
    invalid = 0

    text = io.TextIOWrapper(parsing.SeeklessReader(fh), encoding="utf-8-sig", errors="replace")
    for line in text:
        parts = line.split(delim, 4) if fast else line.split(delim)
        if len(parts) <= max(i_id, i_fuel, i_price, i_self):
            continue
        packed = mapping.get(parts[i_id])
        if packed is None:
            if parts[i_id].strip().isdigit():
                unmapped += 1
            continue
        try:
            price = float(parts[i_price])
        except ValueError:
            invalid += 1
            continue
        if not 0.05 < price < 10.0:
            invalid += 1
            continue
        mode = _self_flag(parts[i_self])
        if mode is None:
            invalid += 1
            continue
        group = _classify(parts[i_fuel], fuel_cache)
        fi = FUEL_INDEX[group]
        rows += 1

        com_idx = packed & 0xFFFFF
        prov_idx = packed >> 20
        ckey = (com_idx * NFUEL + fi) * 2 + mode
        entry = com_acc.get(ckey)
        if entry is None:
            com_acc[ckey] = [1.0, price, price, price]
        else:
            entry[0] += 1.0
            entry[1] += price
            if price < entry[2]:
                entry[2] = price
            elif price > entry[3]:
                entry[3] = price

        pkey = (prov_idx * NFUEL + fi) * 2 + mode
        entry = prov_acc.get(pkey)
        if entry is None:
            prov_acc[pkey] = [1.0, price, price, price]
        else:
            entry[0] += 1.0
            entry[1] += price
            if price < entry[2]:
                entry[2] = price
            elif price > entry[3]:
                entry[3] = price
    text.detach()

    return {
        "date": day.isoformat(),
        "geo": geo_key,
        "rows": rows,
        "unmapped": unmapped,
        "invalid": invalid,
        "prov": _serialize(prov_acc, geo.provinces),
        "com": _serialize(com_acc, geo.comuni),
    }


def _serialize(acc: dict[int, list[float]], names: list[str]) -> dict[str, dict[str, list[float]]]:
    """Converte l'accumulatore indicizzato in ``{area: {combo: [n, media, min, max]}}``."""
    out: dict[str, dict[str, list[float]]] = {}
    for key, (count, total, lowest, highest) in acc.items():
        area_idx, rest = divmod(key, NFUEL * 2)
        fuel_idx, mode = divmod(rest, 2)
        if area_idx >= len(names):
            continue
        combo = combo_key(config.FUEL_ORDER[fuel_idx], mode)
        out.setdefault(names[area_idx], {})[combo] = [
            int(count),
            round(total / count, 4),
            round(lowest, 3),
            round(highest, 3),
        ]
    return out


# --------------------------------------------------------------------------------------
# Cache su disco
# --------------------------------------------------------------------------------------


def day_cache_path(cache_dir: Path, day: date) -> Path:
    return cache_dir / "day" / f"{day.isoformat()}.json.gz"


def load_day_cache(cache_dir: Path, day: date, geo_key: str | None = None) -> dict[str, object] | None:
    path = day_cache_path(cache_dir, day)
    if not path.exists():
        return None
    try:
        payload = util.read_json_gz(path)
    except Exception:  # noqa: BLE001 - cache corrotta: si rigenera
        return None
    if payload.get("v") != CACHE_VERSION:
        return None
    if geo_key is not None and payload.get("geo") != geo_key:
        return None
    return payload


def save_day_cache(cache_dir: Path, payload: dict[str, object]) -> None:
    payload = dict(payload)
    payload["v"] = CACHE_VERSION
    util.write_json_gz(day_cache_path(cache_dir, date.fromisoformat(str(payload["date"]))), payload)


def iter_quarter_price_days(archive: Path) -> Iterator[tuple[date, IO[bytes]]]:
    return parsing.iter_price_days(archive, must_contain="prezzo")


def first_member_station_ids(archive: Path, limit: int = 1) -> set[str]:
    """Legge i primi ``limit`` file giornalieri di un archivio e restituisce gli id impianto.

    Serve a stimare la copertura dell'anagrafica disponibile prima di elaborare il
    trimestre per intero.
    """
    ids: set[str] = set()
    seen = 0
    for _day, fh in iter_quarter_price_days(archive):
        try:
            delim, idx = parsing.price_columns(fh)
        except ValueError:
            continue
        i_id = idx.get("id", 0)
        text = io.TextIOWrapper(parsing.SeeklessReader(fh), encoding="utf-8-sig", errors="replace")
        for line in text:
            parts = line.split(delim, 1)
            if parts and parts[0].strip().isdigit():
                ids.add(parts[i_id].strip() if i_id == 0 else parts[0].strip())
        text.detach()
        seen += 1
        if seen >= limit:
            break
    return ids
