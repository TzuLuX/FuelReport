"""Interfaccia a riga di comando del tool.

Comandi disponibili:

* ``report`` (predefinito) — genera il report HTML;
* ``sync`` — scarica una volta sola gli archivi trimestrali completi (immutabili);
* ``list`` — elenca i trimestri pubblicati e quelli già presenti in cache;
* ``publish`` — pubblica un report già generato su GitHub Pages.

Esempi::

    python3 fuel_report.py --quarters 4
    python3 fuel_report.py sync --from 2015Q1 --to 2026Q2 --jobs 3
    python3 fuel_report.py report --from 2024Q1 --to 2026Q2 --no-stations
    python3 fuel_report.py list
    GITHUB_TOKEN=… python3 fuel_report.py publish report_carburanti.html --repo utente/FuelReport
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config, github, sources, util
from .pipeline import Options, run

TOOL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = TOOL_DIR / "data"
COMMANDS = ("report", "sync", "list", "publish")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help=f"cartella di cache per download e dati intermedi (default: {DEFAULT_DATA_DIR})")
    parser.add_argument("-q", "--quiet", action="store_true", help="output ridotto")


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-n", "--quarters", type=int, default=4,
                        help="numero di trimestri più recenti da considerare (default: 4)")
    parser.add_argument("--from", dest="start", metavar="YYYYQn", default=None,
                        help="primo trimestre (es. 2023Q1)")
    parser.add_argument("--to", dest="end", metavar="YYYYQn", default=None,
                        help="ultimo trimestre (es. 2026Q2)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuel_report.py",
        description=(
            "Report HTML sugli andamenti dei prezzi dei carburanti in Italia dagli open data MIMIT, "
            "con mappa interattiva OpenStreetMap e drilldown geografico "
            "(Italia → regioni → province → comuni → impianti)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "esempi:\n"
            "  python3 fuel_report.py --quarters 4\n"
            "  python3 fuel_report.py sync --from 2015Q1 --to 2026Q2   # download completo una tantum\n"
            "  python3 fuel_report.py report --from 2020Q1 --to 2026Q2 --no-stations\n"
            "  python3 fuel_report.py list\n"
        ),
    )
    parser.add_argument("--version", action="version", version="mimit-fuel-report 1.0.0")
    sub = parser.add_subparsers(dest="command")

    p_report = sub.add_parser("report", help="genera il report HTML (comando predefinito)")
    _add_selection(p_report)
    _add_common(p_report)
    p_report.add_argument("-o", "--out", type=Path, default=Path("report_carburanti.html"),
                          help="file HTML di output (default: report_carburanti.html)")
    p_report.add_argument("--anagrafica", choices=("auto", "live", "quarter"), default="auto",
                          help="anagrafica impianti: auto (default), live o quella del trimestre")
    p_report.add_argument("--coverage", type=float, default=0.99, metavar="0..1",
                          help="soglia di copertura sotto la quale usare l'anagrafica del trimestre (default: 0.99)")
    p_report.add_argument("--refresh", action="store_true",
                          help="ignora le cache di aggregazione e ricalcola tutto")
    p_report.add_argument("--redownload", action="store_true",
                          help="riscarica gli archivi trimestrali anche se già presenti")
    p_report.add_argument("--no-stations", action="store_true",
                          help="non includere l'elenco degli impianti dell'ultimo giorno")
    p_report.add_argument("--no-boundaries", action="store_true",
                          help="non scaricare i confini OpenStreetMap (mappa senza poligoni)")
    p_report.add_argument("--no-live", action="store_true",
                          help="non usare il feed giornaliero per estendere le serie oltre l'ultimo trimestre")
    p_report.add_argument("--plain", action="store_true",
                          help="incorpora i dati non compressi (browser senza DecompressionStream)")
    p_report.add_argument("--tiles", metavar="URL", default=None,
                          help="URL dei tile della mappa di base (default: tile.openstreetmap.org, "
                               "con fallback automatico su altri provider OSM)")

    p_sync = sub.add_parser("sync", help="scarica gli archivi trimestrali completi (una tantum)")
    _add_selection(p_sync)
    _add_common(p_sync)
    p_sync.add_argument("--jobs", type=int, default=3, help="download in parallelo (default: 3)")
    p_sync.add_argument("--only", choices=config.CATEGORIES, action="append", default=None,
                        help="limita il download a una categoria (ripetibile)")

    p_list = sub.add_parser("list", help="elenca i trimestri pubblicati e presenti in cache")
    _add_common(p_list)

    p_publish = sub.add_parser("publish", help="pubblica un report su GitHub Pages (token in GITHUB_TOKEN)")
    p_publish.add_argument("html", type=Path, help="report da pubblicare, es. data/site/index.html")
    p_publish.add_argument("--repo", default=os.environ.get("GITHUB_REPO"), metavar="UTENTE/NOME",
                           help="repository GitHub (default: $GITHUB_REPO)")
    p_publish.add_argument("--branch", default="gh-pages",
                           help="ramo servito da GitHub Pages, sostituito a ogni pubblicazione (default: gh-pages)")

    return parser


def _quarters_from_args(args, available: dict[str, dict[str, str]]) -> list[sources.Quarter]:
    try:
        start = sources.Quarter.parse(args.start) if getattr(args, "start", None) else None
        end = sources.Quarter.parse(args.end) if getattr(args, "end", None) else None
    except ValueError as exc:
        raise SystemExit(f"errore: {exc}")
    return sources.select_quarters(
        available, last=None if start else max(1, getattr(args, "quarters", 4)), start=start, end=end
    )


def _command_report(args) -> int:
    try:
        available = sources.discover_quarters(quiet=args.quiet)
        quarters = _quarters_from_args(args, available)
        if not quarters:
            raise RuntimeError("nessun archivio trimestrale disponibile")
    except Exception as exc:  # noqa: BLE001
        util.log(f"errore: {exc}", "err")
        return 1

    options = Options(
        data_dir=args.data_dir.expanduser().resolve(),
        output=args.out.expanduser(),
        quarters=None if args.start else max(1, args.quarters),
        start=sources.Quarter.parse(args.start) if args.start else None,
        end=sources.Quarter.parse(args.end) if args.end else None,
        anagrafica=args.anagrafica,
        coverage=args.coverage,
        refresh=args.refresh,
        redownload=args.redownload,
        stations=not args.no_stations,
        boundaries=not args.no_boundaries,
        live=not args.no_live,
        plain=args.plain,
        tiles=args.tiles,
        quiet=args.quiet,
        available=available,
    )
    try:
        result = run(options)
    except KeyboardInterrupt:
        print("\ninterrotto dall'utente", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        util.log(f"errore: {exc}", "err")
        return 1
    meta = result.payload.get("meta", {})
    if not args.quiet:
        print()
        util.log(
            f"completato: {result.days} giornate, {meta.get('provinces', 0)} province, "
            f"{meta.get('comuni', 0)} comuni",
            "ok",
        )
        if result.html_path:
            util.log(f"report: {result.html_path} ({util.human_bytes(result.html_bytes)})", "ok")
    return 0


def _command_sync(args) -> int:
    util.set_verbose(not args.quiet)
    cache_dir = args.data_dir.expanduser().resolve() / "cache"
    try:
        available = sources.discover_quarters(quiet=args.quiet)
    except Exception as exc:  # noqa: BLE001
        util.log(f"errore: {exc}", "err")
        return 1
    quarters = _quarters_from_args(args, available)
    if not quarters:
        util.log("nessun archivio da scaricare", "warn")
        return 0
    categories = tuple(args.only) if args.only else config.CATEGORIES
    util.log(
        f"download completo di {len(quarters)} trimestri "
        f"({', '.join(q.key for q in quarters)}) · categorie: {', '.join(categories)}",
        "step",
    )
    stats = sources.sync_archives(
        quarters,
        cache_dir=cache_dir,
        urls=available,
        categories=categories,
        jobs=args.jobs,
        quiet=args.quiet,
    )
    util.log(
        f"archivi scaricati: {stats['downloaded']}, già presenti: {stats['skipped']}, "
        f"totale in cache: {util.human_bytes(stats['bytes'])}",
        "ok" if not stats.get("errors") else "warn",
    )
    util.log(f"cache: {cache_dir}", "info")
    return 1 if stats.get("errors") else 0


def _command_list(args) -> int:
    util.set_verbose(not args.quiet)
    available = sources.discover_quarters(quiet=args.quiet)
    manifest = sources.load_manifest(args.data_dir.expanduser().resolve() / "cache")
    print(f"{'trimestre':<11} {'prezzi':<8} {'anagrafica':<12} {'in cache':<24} {'dimensione':>11}")
    total = 0
    for key in sorted(available, key=sources.Quarter.parse):
        entry = available[key]
        local = manifest.get(key, {})
        cells = []
        for category in config.CATEGORIES:
            info = local.get(category)
            if info:
                cells.append(f"{category.split('_')[0]}:{util.human_bytes(int(info['bytes']))}")
                total += int(info["bytes"])
        print(
            f"{key:<11} {'sì' if entry.get('prezzo_alle_8') else 'no':<8} "
            f"{'sì' if entry.get('anagrafica_impianti_attivi') else 'no':<12} "
            f"{', '.join(cells) if cells else '–':<24}"
        )
    print(f"\ntrimestri pubblicati: {len(available)} · dati in cache: {util.human_bytes(total)}")
    print(f"sorgente: {config.SOURCE_PAGE}")
    return 0


def _command_publish(args) -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token or not args.repo:
        util.log("errore: servono la variabile GITHUB_TOKEN e --repo (o GITHUB_REPO)", "err")
        return 1
    try:
        sha = github.publish_pages(args.html, args.repo, token, args.branch)
    except (OSError, ValueError, RuntimeError) as exc:
        util.log(f"errore: {exc}", "err")
        return 1
    util.log(f"report pubblicato su {args.repo}, ramo {args.branch} ({sha[:7]})", "ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["report", *argv]
    elif not argv:
        argv = ["report"]
    elif argv[0].startswith("-"):
        argv = ["report", *argv]

    args = build_parser().parse_args(argv)
    if args.command == "sync":
        return _command_sync(args)
    if args.command == "list":
        return _command_list(args)
    if args.command == "publish":
        return _command_publish(args)
    return _command_report(args)
