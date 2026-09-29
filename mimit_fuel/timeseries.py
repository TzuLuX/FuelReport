"""Costruzione delle serie storiche per il report a partire dalle cache giornaliere.

Le cache giornaliere contengono statistiche per provincia e per comune; da queste si
ottengono tutte le serie necessarie al report:

* livello nazionale e regionale: aggregazione delle province (partizioni disgiunte, quindi
  media/min/max esatti);
* livello provinciale: serie giornaliere (o settimanali per periodi molto lunghi);
* livello comunale: serie mensili (o trimestrali per periodi molto lunghi);
* banda di dispersione (p10-p90) calcolata tra le province, giorno per giorno.

La granularità si adatta automaticamente alla lunghezza del periodo per mantenere il
report caricabile.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

from . import config

#: oltre questa durata le serie provinciali diventano settimanali
DAILY_MAX_DAYS = 420
#: oltre questo numero di mesi le serie comunali diventano trimestrali
MONTHLY_MAX_MONTHS = 72


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def quarter_key(day: date) -> str:
    return f"{day.year:04d}-T{(day.month - 1) // 3 + 1}"


def month_range(start: date, end: date) -> list[str]:
    keys: list[str] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        keys.append(month_key(cursor))
        year, month = (cursor.year + 1, 1) if cursor.month == 12 else (cursor.year, cursor.month + 1)
        cursor = date(year, month, 1)
    return keys


def quarter_range(start: date, end: date) -> list[str]:
    keys: list[str] = []
    cursor = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    while cursor <= end:
        keys.append(quarter_key(cursor))
        month = cursor.month + 3
        year = cursor.year + (month - 1) // 12
        cursor = date(year, (month - 1) % 12 + 1, 1)
    return keys


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _r4(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


class _Accumulator:
    """Medie pesate (per numero di impianti) su un asse temporale."""

    __slots__ = ("counts", "sums")

    def __init__(self, size: int):
        self.counts = [0] * size
        self.sums = [0.0] * size

    def add(self, index: int, count: int, mean: float) -> None:
        self.counts[index] += count
        self.sums[index] += count * mean

    def mean(self, index: int) -> float | None:
        count = self.counts[index]
        return self.sums[index] / count if count else None

    def series(self) -> list[float | None]:
        return [_r4(self.mean(i)) for i in range(len(self.counts))]

    def count_series(self) -> list[int]:
        return list(self.counts)

    def resample(self, size: int, positions: list[int]) -> "_Accumulator":
        """Ricampiona l'asse giornaliero su un asse più lungo (settimane, mesi, trimestri)."""
        target = _Accumulator(size)
        for index, count in enumerate(self.counts):
            if count:
                target.add(positions[index], count, self.sums[index] / count)
        return target


def build_payload(
    day_payloads: Iterable[dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
    comuni_meta: dict[str, dict[str, Any]],
    meta: dict[str, Any],
    granularity_end: date | None = None,
) -> dict[str, Any]:
    """Costruisce il payload JSON che alimenta il report HTML.

    ``day_payloads`` viene consumato in streaming (ordine cronologico): in questo modo
    anche analisi pluriennali non saturano la memoria.

    Granularità delle serie: nazionale e regionale sempre giornaliere; provinciali
    giornaliere fino a ``DAILY_MAX_DAYS`` giorni (poi settimanali); comunali mensili fino a
    ``MONTHLY_MAX_MONTHS`` mesi (poi trimestrali). La durata si misura fino a
    ``granularity_end`` (l'ultimo giorno degli archivi trimestrali), così i pochi giorni del
    feed giornaliero aggiunti in coda non cambiano la granularità delle serie.
    """
    days = daterange(period_start, period_end)
    day_index = {day.isoformat(): i for i, day in enumerate(days)}
    threshold_end = min(granularity_end or period_end, period_end)
    provinces_weekly = (threshold_end - period_start).days + 1 > DAILY_MAX_DAYS

    week_index: dict[date, int] = {}
    if provinces_weekly:
        cursor = week_start(days[0])
        position = 0
        while cursor <= days[-1]:
            week_index[cursor] = position
            position += 1
            cursor += timedelta(days=7)
    week_of_day = (
        [week_index[week_start(day)] for day in days] if provinces_weekly else list(range(len(days)))
    )
    axis_size = len(days)
    dates_daily = [day.isoformat() for day in days]
    dates_provinces = [day.isoformat() for day in sorted(week_index)] if provinces_weekly else dates_daily

    quarterly = len(month_range(period_start, threshold_end)) > MONTHLY_MAX_MONTHS
    months = quarter_range(period_start, period_end) if quarterly else month_range(period_start, period_end)
    month_index = {key: i for i, key in enumerate(months)}
    month_of_day = [month_index[quarter_key(day) if quarterly else month_key(day)] for day in days]

    province_acc: dict[str, dict[str, _Accumulator]] = defaultdict(dict)
    comune_acc: dict[str, dict[str, _Accumulator]] = defaultdict(dict)
    combos_seen: set[str] = set()
    total_rows = 0
    total_unmapped = 0
    n_days = 0
    first_day: str | None = None
    last_day_date: str | None = None
    last_day: dict[str, Any] | None = None

    for payload in day_payloads:
        day = date.fromisoformat(str(payload["date"]))
        if not (period_start <= day <= period_end):
            continue
        index = day_index[day.isoformat()]
        month_position = month_of_day[index]
        total_rows += int(payload.get("rows", 0))
        total_unmapped += int(payload.get("unmapped", 0))
        n_days += 1
        first_day = first_day or day.isoformat()
        if last_day_date is None or day.isoformat() > last_day_date:
            last_day_date = day.isoformat()
            last_day = payload

        for province, combos in payload.get("prov", {}).items():
            target = province_acc[province]
            for combo, values in combos.items():
                combos_seen.add(combo)
                accumulator = target.get(combo)
                if accumulator is None:
                    accumulator = target[combo] = _Accumulator(axis_size)
                accumulator.add(index, int(values[0]), float(values[1]))

        for comune, combos in payload.get("com", {}).items():
            target = comune_acc[comune]
            for combo, values in combos.items():
                accumulator = target.get(combo)
                if accumulator is None:
                    accumulator = target[combo] = _Accumulator(len(months))
                accumulator.add(month_position, int(values[0]), float(values[1]))

    # ---- serie provinciali + rollup regionale e nazionale -------------------------
    provinces_payload: dict[str, dict[str, list[float | None]]] = {}
    provinces_counts: dict[str, dict[str, list[int]]] = {}
    provinces_last: dict[str, dict[str, list[float]]] = {}
    region_acc: dict[str, dict[str, _Accumulator]] = defaultdict(dict)
    italy_acc: dict[str, _Accumulator] = {}

    def export_province(accumulator: _Accumulator) -> tuple[list[float | None], list[int]]:
        """Serie provinciale alla granularità scelta (giornaliera o settimanale)."""
        if not provinces_weekly:
            return accumulator.series(), accumulator.count_series()
        weekly_acc = accumulator.resample(len(week_index), week_of_day)
        return weekly_acc.series(), weekly_acc.count_series()

    for province, combos in province_acc.items():
        region = config.PROVINCE_TO_REGION.get(province, "nd")
        provinces_payload[province] = {}
        for combo, accumulator in combos.items():
            series, counts = export_province(accumulator)
            provinces_payload[province][combo] = series
            provinces_counts.setdefault(province, {})[combo] = counts
            region_accumulator = region_acc[region].get(combo)
            if region_accumulator is None:
                region_accumulator = region_acc[region][combo] = _Accumulator(axis_size)
            italy_accumulator = italy_acc.get(combo)
            if italy_accumulator is None:
                italy_accumulator = italy_acc[combo] = _Accumulator(axis_size)
            for index, count in enumerate(accumulator.counts):
                if count:
                    region_accumulator.add(index, count, accumulator.sums[index] / count)
                    italy_accumulator.add(index, count, accumulator.sums[index] / count)

    italy_payload = {combo: acc.series() for combo, acc in italy_acc.items()}
    italy_counts = {combo: acc.count_series() for combo, acc in italy_acc.items()}
    # serie nazionale sugli assi di province e comuni, per confrontarla con le aree locali
    italy_axes: dict[str, dict[str, list[float | None]]] = {
        "comuni": {combo: acc.resample(len(months), month_of_day).series() for combo, acc in italy_acc.items()},
    }
    if provinces_weekly:
        italy_axes["provinces"] = {
            combo: acc.resample(len(week_index), week_of_day).series() for combo, acc in italy_acc.items()
        }
    regions_payload = {region: {c: a.series() for c, a in combos.items()} for region, combos in region_acc.items()}
    region_counts = {region: {c: a.count_series() for c, a in combos.items()} for region, combos in region_acc.items()}

    if last_day:
        for province, combos in last_day.get("prov", {}).items():
            provinces_last[province] = {
                combo: [round(float(values[1]), 4), int(values[0])] for combo, values in combos.items()
            }

    # ---- banda di dispersione tra province (asse giornaliero) --------------------
    dispersion: dict[str, dict[str, list[float | None]]] = {}
    for combo, accumulator in italy_acc.items():
        low: list[float | None] = []
        mid: list[float | None] = []
        high: list[float | None] = []
        for index in range(axis_size):
            values = [
                combos[combo].sums[index] / combos[combo].counts[index]
                for combos in province_acc.values()
                if combo in combos and combos[combo].counts[index]
            ]
            if len(values) >= 3:
                low.append(round(_percentile(values, 0.10), 4))
                mid.append(round(statistics.median(values), 4))
                high.append(round(_percentile(values, 0.90), 4))
            else:
                low.append(None)
                mid.append(None)
                high.append(None)
        dispersion[combo] = {"p10": low, "p50": mid, "p90": high}

    # ---- serie comunali -----------------------------------------------------------
    comuni_keys = sorted(comune_acc)
    comuni_index = {key: i for i, key in enumerate(comuni_keys)}
    comuni_monthly: dict[str, list[list[float | None] | None]] = {}
    comuni_last: dict[str, list[list[float] | None]] = {}
    for combo in sorted(combos_seen):
        monthly: list[list[float | None] | None] = [None] * len(comuni_keys)
        latest: list[list[float] | None] = [None] * len(comuni_keys)
        for comune, combos in comune_acc.items():
            accumulator = combos.get(combo)
            if accumulator is None:
                continue
            position = comuni_index[comune]
            monthly[position] = accumulator.series()
            if last_day:
                values = last_day.get("com", {}).get(comune, {}).get(combo)
                if values:
                    latest[position] = [round(float(values[1]), 4), int(values[0])]
        comuni_monthly[combo] = monthly
        comuni_last[combo] = latest

    comuni_payload = {
        key: {
            "prov": str(comuni_meta.get(key, {}).get("prov", key.split("|")[0])),
            "nome": str(comuni_meta.get(key, {}).get("nome", key.split("|")[-1])),
            "lat": comuni_meta.get(key, {}).get("lat"),
            "lon": comuni_meta.get(key, {}).get("lon"),
            "n": int(comuni_meta.get(key, {}).get("n", 0)),
        }
        for key in comuni_keys
    }

    meta = dict(meta)
    meta.update(
        {
            "period": [period_start.isoformat(), period_end.isoformat()],
            "points": {"daily": len(dates_daily), "provinces": len(dates_provinces), "comuni": len(months)},
            "axes": {"it": "daily", "regions": "daily", "provinces": "provinces", "comuni": "comuni"},
            "granularity": {
                "it": "giornaliere",
                "regions": "giornaliere",
                "provinces": "settimanali" if provinces_weekly else "giornaliere",
                "comuni": "trimestrali" if quarterly else "mensili",
            },
            "combos": sorted(combos_seen),
            "rows": total_rows,
            "unmapped": total_unmapped,
            "days": n_days,
            "first_day": first_day,
            "last_day": last_day_date,
            "comuni": len(comuni_keys),
            "provinces": len(provinces_payload),
            "regions": len(regions_payload),
            "fuels": [
                {"name": name, "description": config.FUEL_DESCRIPTIONS.get(name, "")}
                for name in config.FUEL_ORDER
                if any(combo.startswith(name + "|") for combo in combos_seen)
            ],
        }
    )

    return {
        "meta": meta,
        "dates": {"daily": dates_daily, "provinces": dates_provinces, "comuni": months},
        "italy": italy_payload,
        "italy_counts": italy_counts,
        "italy_axes": italy_axes,
        "regions": regions_payload,
        "region_counts": region_counts,
        "provinces": provinces_payload,
        "provinces_last": provinces_last,
        "dispersion": dispersion,
        "comuni_keys": comuni_keys,
        "comuni_meta": comuni_payload,
        "comuni_monthly": comuni_monthly,
        "comuni_last": comuni_last,
    }


def _coord(value: str) -> float | None:
    """Converte una coordinata dell'anagrafica MIMIT (a volte vuota o "NA")."""
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if -180.0 <= number <= 180.0 else None


def station_payload(
    stations: dict[str, list[str]],
    prices: dict[str, dict[str, float]],
    *,
    day: str,
    comuni_of_station: dict[str, str],
    limit_per_comune: int = 400,
) -> dict[str, Any]:
    """Prepara l'elenco degli impianti (con i prezzi dell'ultimo giorno) per comune."""
    per_comune: dict[str, list[list[Any]]] = defaultdict(list)
    for sid, combos in prices.items():
        comune = comuni_of_station.get(sid)
        record = stations.get(sid)
        if not comune or not record:
            continue
        lat, lon = _coord(record[7]), _coord(record[8])
        per_comune[comune].append(
            [
                sid,
                (record[3] or record[0]).strip(),
                record[1].strip(),
                record[4].strip(),
                round(lat, 5) if lat is not None else None,
                round(lon, 5) if lon is not None else None,
                {combo: round(price, 3) for combo, price in sorted(combos.items())},
            ]
        )
    return {
        "day": day,
        "comuni": {comune: rows[:limit_per_comune] for comune, rows in per_comune.items()},
    }


def summarize(day_payloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Statistiche di sintesi sulle giornate (usata per i log e per il payload)."""
    days = [str(p["date"]) for p in day_payloads]
    rows = sum(int(p.get("rows", 0)) for p in day_payloads)
    unmapped = sum(int(p.get("unmapped", 0)) for p in day_payloads)
    return {
        "days": len(days),
        "first": min(days) if days else None,
        "last": max(days) if days else None,
        "rows": rows,
        "unmapped": unmapped,
    }
