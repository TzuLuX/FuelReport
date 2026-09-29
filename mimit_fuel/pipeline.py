"""Orchestrazione: download degli archivi, aggregazione, costruzione payload e report."""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from . import aggregate, config, parsing, sources, timeseries, util
from .parsing import GeoIndex

GEO_CACHE_VERSION = 3
#: sotto questa quota di impianti riconosciuti si scarica l'anagrafica del trimestre
COVERAGE_THRESHOLD = 0.99


@dataclass
class Options:
    """Opzioni di esecuzione del tool."""

    data_dir: Path
    output: Path
    quarters: int | None = 4
    start: sources.Quarter | None = None
    end: sources.Quarter | None = None
    anagrafica: str = "auto"
    coverage: float = COVERAGE_THRESHOLD
    refresh: bool = False
    redownload: bool = False
    stations: bool = True
    boundaries: bool = True
    live: bool = True
    plain: bool = False
    tiles: str | None = None
    render: bool = True
    quiet: bool = False
    available: dict[str, dict[str, str]] | None = None


@dataclass
class Result:
    """Esito dell'esecuzione, usato dalla CLI per il riepilogo finale."""

    payload: dict[str, Any] = field(default_factory=dict)
    quarters: list[str] = field(default_factory=list)
    days: int = 0
    html_path: Path | None = None
    payload_bytes: int = 0
    html_bytes: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Cache dell'indice geografico
# --------------------------------------------------------------------------------------


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}-{int(stat.st_mtime)}"


def load_or_build_geo(
    cache_dir: Path,
    key: str,
    fingerprint: str,
    builder: Callable[[], GeoIndex],
    *,
    refresh: bool = False,
) -> GeoIndex:
    """Carica (o costruisce e mette in cache) l'indice geografico di un'anagrafica."""
    path = cache_dir / "ana" / f"{key.replace(':', '_')}.json.gz"
    if path.exists() and not refresh:
        try:
            payload = util.read_json_gz(path)
            if payload.get("version") == GEO_CACHE_VERSION and payload.get("fingerprint") == fingerprint:
                return GeoIndex.from_dict(payload["geo"])
        except Exception:  # noqa: BLE001 - cache non valida: si ricostruisce
            pass
    geo = builder()
    util.write_json_gz(
        path,
        {"version": GEO_CACHE_VERSION, "fingerprint": fingerprint, "geo": geo.to_dict()},
        level=9,
    )
    return geo


def _anagrafica_extraction_date(path: Path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(200).decode("utf-8-sig", errors="replace")
    for line in head.splitlines():
        if "strazione" in line:
            day = parsing.extraction_date(line)
            if day:
                return day.isoformat()
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------------------
# Estrazione dei prezzi per singolo impianto (per la tabella degli impianti)
# --------------------------------------------------------------------------------------


def read_day_prices(archive: Path, day: date) -> dict[str, dict[str, float]]:
    """Legge un singolo giorno dell'archivio e restituisce ``{idImpianto: {combo: prezzo}}``."""
    stamp = day.strftime("%Y%m%d")
    prices: dict[str, dict[str, float]] = {}
    with tarfile.open(archive, "r:gz") as tar:
        members = [m.name for m in tar.getmembers() if m.isfile() and stamp in m.name and "prezzo" in m.name]
        for name in members:
            fh = tar.extractfile(name)
            if fh is not None:
                _collect_prices(fh, prices)
    return prices


def read_prices_csv(path: Path) -> dict[str, dict[str, float]]:
    """Legge un CSV di prezzi (es. il feed giornaliero "live")."""
    prices: dict[str, dict[str, float]] = {}
    with open(path, "rb") as fh:
        _collect_prices(fh, prices)
    return prices


def _collect_prices(fh, prices: dict[str, dict[str, float]]) -> None:
    """Estrae da un CSV prezzi ``{idImpianto: {combo: prezzo}}`` (una riga per impianto/carburante)."""
    fuel_cache: dict[str, str] = {}
    delim, idx = parsing.price_columns(fh)
    i_id, i_fuel, i_price, i_self = (
        idx.get("id", 0),
        idx.get("carburante", 1),
        idx.get("prezzo", 2),
        idx.get("self", 3),
    )
    text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
    for line in text:
        parts = line.split(delim)
        if len(parts) <= max(i_id, i_fuel, i_price, i_self):
            continue
        sid = parts[i_id].strip()
        if not sid.isdigit():
            continue
        try:
            price = float(parts[i_price])
        except ValueError:
            continue
        mode = aggregate._self_flag(parts[i_self])
        if mode is None or not 0.05 < price < 10.0:
            continue
        group = fuel_cache.get(parts[i_fuel]) or config.classify_fuel(parts[i_fuel])
        fuel_cache[parts[i_fuel]] = group
        prices.setdefault(sid, {})[aggregate.combo_key(group, mode)] = price
    text.detach()


def live_feed_applies(quarters: list[sources.Quarter], available: dict[str, dict[str, str]]) -> bool:
    """Il feed giornaliero estende le serie solo se l'ultimo trimestre scelto è il più recente.

    Con ``--to`` su un trimestre precedente il report deve fermarsi lì: aggiungere il feed
    includerebbe i trimestri successivi presenti in cache, oppure indicherebbe come "non
    ancora pubblicati" periodi che il MIMIT ha già pubblicato.
    """
    published = [sources.Quarter.parse(key) for key in available]
    return bool(quarters) and bool(published) and quarters[-1] >= max(published)


def ingest_live_prices(cache_dir: Path, geo_live: GeoIndex, *, quiet: bool = False) -> date | None:
    """Aggrega lo snapshot giornaliero dei prezzi e lo aggiunge alla cache delle giornate.

    Il MIMIT pubblica ogni giorno una fotografia dei prezzi comunicati ("prezzo alle 8"):
    conservarla permette di estendere il report oltre l'ultimo trimestre archiviato, che
    viene pubblicato con alcuni mesi di ritardo.
    """
    try:
        path = sources.fetch_live("prezzo_alle_8", cache_dir=cache_dir)
    except Exception as exc:  # noqa: BLE001
        util.log(f"feed giornaliero non disponibile: {exc}", "warn")
        return None
    day = parsing.file_extraction_date(path)
    if day is None:
        util.log("feed giornaliero senza data di estrazione: ignorato", "warn")
        return None
    with open(path, "rb") as fh:
        payload = aggregate.aggregate_day(day, fh, geo_live, {}, "live")
    if not payload.get("rows"):
        return None
    payload["source"] = "live"
    aggregate.save_day_cache(cache_dir, payload)
    util.log(f"feed giornaliero del {day}: {payload['rows']:,} prezzi aggiunti al report".replace(",", "."), "ok")
    return day


# --------------------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------------------


def run(options: Options) -> Result:
    """Esegue l'intera pipeline e (opzionalmente) scrive il report HTML."""
    util.set_verbose(not options.quiet)
    result = Result()
    cache_dir = util.ensure_dir(options.data_dir / "cache")

    # ---- 1. scoperta degli archivi -------------------------------------------------
    if options.available:
        available = options.available
    else:
        util.log("lettura della pagina open data MIMIT", "step")
        available = sources.discover_quarters(quiet=options.quiet)
    quarters = sources.select_quarters(
        available, last=options.quarters, start=options.start, end=options.end
    )
    if not quarters:
        raise RuntimeError("nessun archivio trimestrale disponibile")
    util.log(
        f"trimestri selezionati: {', '.join(q.key for q in quarters)} ({len(quarters)})",
        "ok",
    )
    result.quarters = [q.key for q in quarters]

    # ---- 2. anagrafica "live" ------------------------------------------------------
    live_csv = sources.fetch_live("anagrafica_impianti_attivi", cache_dir=cache_dir)
    live_stamp = _anagrafica_extraction_date(live_csv)
    geo_live = load_or_build_geo(
        cache_dir,
        "live",
        _fingerprint(live_csv),
        lambda: GeoIndex(parsing.read_anagrafica_file(live_csv)),
        refresh=options.refresh,
    )
    util.log(
        f"anagrafica live del {live_stamp}: {len(geo_live.mapping)} impianti geolocalizzati"
        f" ({geo_live.unresolved} senza provincia, {geo_live.without_coords} senza coordinate)",
        "ok",
    )
    result.notes.append(
        f"anagrafica impianti: snapshot live {live_stamp} "
        f"({len(geo_live.mapping)} impianti, {geo_live.unresolved} non attribuibili a una provincia)"
    )

    # ---- 3. elaborazione dei trimestri --------------------------------------------
    quarters_used: list[tuple[sources.Quarter, GeoIndex, Path]] = []
    for quarter in quarters:
        geo, archive = _process_quarter(quarter, available.get(quarter.key, {}), options, cache_dir, geo_live)
        quarters_used.append((quarter, geo, archive))

    # ---- 3b. feed giornaliero dei prezzi -------------------------------------------
    live_day = None
    if options.live:
        if live_feed_applies(quarters, available):
            live_day = ingest_live_prices(cache_dir, geo_live, quiet=options.quiet)
        else:
            util.log(
                f"feed giornaliero non usato: {quarters[-1].key} non è l'ultimo trimestre pubblicato",
                "info",
            )

    # ---- 4. raccolta delle giornate -------------------------------------------------
    period_start = quarters[0].start
    archive_end = quarters[-1].end
    period_end = archive_end
    if live_day and live_day > archive_end:
        period_end = live_day
    else:
        live_day = None
    comuni_meta: dict[str, dict[str, Any]] = {}
    for _quarter, geo, _archive in quarters_used:
        for key, meta in geo.comuni_meta.items():
            comuni_meta[key] = meta

    def day_stream() -> Iterator[dict[str, Any]]:
        """Le giornate vengono lette dalla cache in streaming (una alla volta)."""
        for day in timeseries.daterange(period_start, period_end):
            payload = aggregate.load_day_cache(cache_dir, day)
            if payload is None:
                continue
            # dopo l'ultimo trimestre selezionato valgono solo i giorni del feed giornaliero
            if day > archive_end and payload.get("source") != "live":
                continue
            yield payload

    # ---- 5. payload delle serie storiche -------------------------------------------
    util.log("costruzione delle serie storiche", "step")
    meta = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source_page": config.SOURCE_PAGE,
        "archives": [q.key for q, _g, _a in quarters_used],
        "archive_end": archive_end.isoformat(),
        "anagrafica": live_stamp,
        "live_day": live_day.isoformat() if live_day else None,
        "osm_attribution": "© OpenStreetMap contributors (ODbL)",
        "notes": result.notes,
    }
    payload = timeseries.build_payload(
        day_stream(),
        period_start=period_start,
        period_end=period_end,
        comuni_meta=comuni_meta,
        meta=meta,
        granularity_end=archive_end,
    )
    payload["meta"]["quarter_labels"] = {q.key: q.label() for q, _g, _a in quarters_used}
    result.payload = payload
    result.days = int(payload["meta"]["days"])
    if not result.days:
        raise RuntimeError("nessuna giornata elaborata: verifica la connessione e riprova")
    util.log(
        f"giornate disponibili: {payload['meta']['days']} "
        f"({payload['meta']['first_day']} → {payload['meta']['last_day']}), "
        f"{payload['meta']['rows']:,} righe di prezzo".replace(",", "."),
        "ok",
    )

    # ---- 6. impianti dell'ultimo giorno --------------------------------------------
    if options.stations and quarters_used:
        quarter, geo, archive = quarters_used[-1]
        last_day = date.fromisoformat(str(payload["meta"]["last_day"] or quarters[-1].end.isoformat()))
        prices: dict[str, dict[str, float]] | None = None
        geo_for_stations = geo
        if live_day and last_day == live_day:
            util.log(f"lettura dei prezzi per impianto dal feed giornaliero del {last_day}", "step")
            try:
                # la stessa copia letta per live_day, senza riscaricarla a metà esecuzione
                prices = read_prices_csv(sources.live_path("prezzo_alle_8", cache_dir))
                geo_for_stations = geo_live
            except Exception as exc:  # noqa: BLE001
                util.log(f"impianti del feed giornaliero non disponibili: {exc}", "warn")
                prices = None
        elif quarter.start <= last_day <= quarter.end:
            util.log(f"lettura dei prezzi per impianto del {last_day}", "step")
            try:
                prices = read_day_prices(archive, last_day)
            except Exception as exc:  # noqa: BLE001
                util.log(f"elenco impianti non disponibile: {exc}", "warn")
                prices = None
        if prices:
            comuni_of_station = {
                sid: geo_for_stations.comuni[packed & 0xFFFFF] for sid, packed in geo_for_stations.mapping.items()
            }
            payload["stations"] = timeseries.station_payload(
                geo_for_stations.stations,
                prices,
                day=last_day.isoformat(),
                comuni_of_station=comuni_of_station,
            )
            util.log(
                f"impianti inclusi nel report: {len(prices)} ({len(payload['stations']['comuni'])} comuni)",
                "ok",
            )

    # ---- 7. confini OSM ------------------------------------------------------------
    if options.boundaries:
        from . import geo as geo_module

        boundaries = geo_module.load_boundaries(cache_dir, refresh=False, quiet=options.quiet)
        if boundaries:
            payload["boundaries"] = boundaries
            util.log(
                f"confini OSM: {sum(1 for f in boundaries['features'] if f['properties']['level'] == 4)} regioni, "
                f"{sum(1 for f in boundaries['features'] if f['properties']['level'] == 6)} province",
                "ok",
            )
        else:
            result.notes.append("confini OpenStreetMap non disponibili: mapa in modalità 'solo marker'")

    # ---- 8. report HTML ------------------------------------------------------------
    if options.render:
        from . import report as report_module

        util.log("generazione del report HTML", "step")
        html, stats = report_module.render(
            payload, data_dir=options.data_dir, quiet=options.quiet, plain=options.plain, tiles=options.tiles
        )
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(html, encoding="utf-8")
        result.html_path = options.output
        result.html_bytes = len(html.encode("utf-8"))
        result.payload_bytes = stats.get("payload_bytes", 0)
        util.log(f"report scritto in {options.output} ({util.human_bytes(result.html_bytes)})", "ok")

    return result


def _process_quarter(
    quarter: sources.Quarter,
    urls: dict[str, str],
    options: Options,
    cache_dir: Path,
    geo_live: GeoIndex,
) -> tuple[GeoIndex, Path]:
    """Elabora (o riusa dalla cache) tutte le giornate di un trimestre."""
    if options.redownload:
        stale = cache_dir / "raw" / f"prezzo_alle_8-{quarter.archive_name}"
        if stale.exists():
            stale.unlink()
    archive = sources.fetch_archive(
        quarter, "prezzo_alle_8", cache_dir=cache_dir, url=urls.get("prezzo_alle_8")
    )

    # scelta dell'anagrafica: quella del trimestre solo se serve davvero
    geo_key = "live"
    geo = geo_live
    needs_quarter_ana = options.anagrafica == "quarter"
    if options.anagrafica == "auto":
        ids = aggregate.first_member_station_ids(archive, limit=1)
        coverage = geo_live.coverage(ids)
        if coverage < options.coverage:
            needs_quarter_ana = True
            util.log(
                f"{quarter.key}: l'anagrafica live copre il {coverage:.1%} degli impianti, "
                "uso l'anagrafica del trimestre",
                "info",
            )
        else:
            util.log(f"{quarter.key}: anagrafica live sufficiente ({coverage:.1%} di copertura)", "info")
    if needs_quarter_ana:
        ana_archive = sources.fetch_archive(
            quarter, "anagrafica_impianti_attivi", cache_dir=cache_dir, url=urls.get("anagrafica_impianti_attivi")
        )
        geo_key = f"quarter:{quarter.key}"
        geo = load_or_build_geo(
            cache_dir,
            f"quarter-{quarter.key}",
            _fingerprint(ana_archive),
            lambda: GeoIndex(parsing.read_anagrafica_archive(ana_archive)),
            refresh=options.refresh,
        )
        util.log(
            f"{quarter.key}: anagrafica del trimestre con {len(geo.mapping)} impianti "
            f"({geo.unresolved} senza provincia)",
            "ok",
        )

    processed_days = 0
    cached_days = 0
    fuel_cache: dict[str, str] = {}
    for day, fh in aggregate.iter_quarter_price_days(archive):
        cached = None if options.refresh else aggregate.load_day_cache(cache_dir, day, geo_key)
        if cached is not None:
            cached_days += 1
            # il generatore deve comunque consumare il file corrente
            fh.read()
            continue
        payload = aggregate.aggregate_day(day, fh, geo, fuel_cache, geo_key)
        aggregate.save_day_cache(cache_dir, payload)
        processed_days += 1
    util.log(
        f"{quarter.key}: {processed_days + cached_days} giornate "
        f"({processed_days} elaborate, {cached_days} da cache)",
        "ok",
    )
    return geo, archive
