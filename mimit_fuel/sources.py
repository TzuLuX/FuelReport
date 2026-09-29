"""Scoperta degli archivi trimestrali e download con cache locale.

La fonte primaria è la pagina open data del MIMIT; in caso di errore si ripiega sul
listing di directory di ``opendatacarburanti.mise.gov.it`` (che espone gli stessi file).
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import config, parsing, util

_ARCHIVE_RE = re.compile(
    r'href="(?P<url>[^"]*/(?P<cat>prezzo_alle_8|anagrafica_impianti_attivi)/(?P<year>\d{4})/'
    r'(?P<file>(?P<y2>\d{4})_(?P<q>[1-4])_tr\.tar\.gz))"'
)


@dataclass(frozen=True, order=True)
class Quarter:
    """Un trimestre dell'archivio storico (es. 2025Q1)."""

    year: int
    quarter: int

    @property
    def key(self) -> str:
        return f"{self.year}Q{self.quarter}"

    @property
    def start(self) -> date:
        return date(self.year, (self.quarter - 1) * 3 + 1, 1)

    @property
    def end(self) -> date:
        last_month = self.quarter * 3
        if last_month == 12:
            return date(self.year, 12, 31)
        return date.fromordinal(date(self.year, last_month + 1, 1).toordinal() - 1)

    @property
    def archive_name(self) -> str:
        return f"{self.year}_{self.quarter}_tr.tar.gz"

    def label(self) -> str:
        return f"{self.quarter}° trimestre {self.year}"

    @classmethod
    def parse(cls, value: str) -> "Quarter":
        text = value.strip().upper().replace("-", "").replace("_", "")
        match = re.fullmatch(r"(\d{4})Q([1-4])", text)
        if match:
            return cls(int(match.group(1)), int(match.group(2)))
        match = re.fullmatch(r"(\d{4})T([1-4])", text)
        if match:
            return cls(int(match.group(1)), int(match.group(2)))
        raise ValueError(f"trimestre non riconosciuto: {value!r} (usa il formato 2025Q1)")

    @classmethod
    def from_date(cls, day: date) -> "Quarter":
        return cls(day.year, (day.month - 1) // 3 + 1)


def archive_url(quarter: Quarter, category: str) -> str:
    return f"{config.ARCHIVE_BASE}/categorized/{category}/{quarter.year}/{quarter.archive_name}"


def _discover_from_directory(quiet: bool = False) -> dict[str, dict[str, str]]:
    """Fallback: elenca i file dai listing di directory del server degli archivi."""
    found: dict[str, dict[str, str]] = {}
    for category in config.CATEGORIES:
        url = f"{config.ARCHIVE_BASE}/categorized/{category}/"
        try:
            listing = util.http_get(url, timeout=60).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            util.log(f"listing non disponibile per {category}: {exc}", "warn")
            continue
        for href in re.findall(r'href="(\d{4})/"', listing):
            year = int(href)
            try:
                sub = util.http_get(f"{url}{year}/", timeout=60).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            for name in re.findall(r'href="(\d{4}_[1-4]_tr\.tar\.gz)"', sub):
                match = re.match(r"(\d{4})_([1-4])_tr", name)
                if not match:
                    continue
                quarter = Quarter(int(match.group(1)), int(match.group(2)))
                found.setdefault(quarter.key, {})[category] = f"{url}{year}/{name}"
    if not quiet:
        util.log(f"scoperti {len(found)} trimestri dal listing di directory", "ok")
    return found


def discover_quarters(quiet: bool = False) -> dict[str, dict[str, str]]:
    """Restituisce ``{trimestre: {categoria: url}}`` leggendo la pagina MIMIT."""
    quarters: dict[str, dict[str, str]] = {}
    try:
        html = util.http_get(config.SOURCE_PAGE, timeout=90).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        util.log(f"pagina open data non raggiungibile ({exc})", "warn")
        html = ""
    for match in _ARCHIVE_RE.finditer(html):
        if match.group("y2") != match.group("year"):
            continue
        quarter = Quarter(int(match.group("year")), int(match.group("q")))
        url = urllib.parse.urljoin(config.SOURCE_PAGE, match.group("url"))
        quarters.setdefault(quarter.key, {})[match.group("cat")] = url
    if quarters:
        if not quiet:
            util.log(f"scoperti {len(quarters)} trimestri dalla pagina MIMIT open data", "ok")
        return quarters
    return _discover_from_directory(quiet=quiet)


def select_quarters(
    available: dict[str, dict[str, str]],
    *,
    last: int | None,
    start: Quarter | None,
    end: Quarter | None,
) -> list[Quarter]:
    """Seleziona i trimestri da elaborare in base alle opzioni della CLI."""
    keys = sorted(Quarter.parse(k) for k in available)
    if not keys:
        return []
    end = end or keys[-1]
    if start is None and last:
        idx = max(0, len([k for k in keys if k <= end]) - last)
        start = keys[idx]
    start = start or keys[0]
    return [k for k in keys if start <= k <= end]


def fetch_archive(
    quarter: Quarter,
    category: str,
    *,
    cache_dir: Path,
    url: str | None = None,
) -> Path:
    """Scarica (una sola volta) l'archivio trimestrale della categoria indicata.

    Gli archivi storici pubblicati dal MIMIT sono immutabili: la dimensione registrata nel
    manifest permette di riconoscere i file già scaricati senza riscaricarli.
    """
    manifest = load_manifest(cache_dir)
    dest = cache_dir / "raw" / f"{category}-{quarter.archive_name}"
    recorded = (manifest.get(quarter.key, {}) or {}).get(category) or {}
    if dest.exists() and dest.stat().st_size > 1_000_000:
        if not recorded.get("bytes") or int(recorded["bytes"]) == dest.stat().st_size:
            if not recorded.get("bytes"):
                manifest.setdefault(quarter.key, {})[category] = {
                    "file": dest.name,
                    "bytes": dest.stat().st_size,
                    "downloaded": time.strftime("%Y-%m-%dT%H:%M"),
                    "url": url or archive_url(quarter, category),
                    "source": "file locale",
                }
                save_manifest(cache_dir, manifest)
            return dest
    url = url or archive_url(quarter, category)
    util.download_file(url, dest)
    manifest.setdefault(quarter.key, {})[category] = {
        "file": dest.name,
        "bytes": dest.stat().st_size,
        "downloaded": time.strftime("%Y-%m-%dT%H:%M"),
        "url": url,
    }
    save_manifest(cache_dir, manifest)
    return dest


def live_path(category: str, cache_dir: Path) -> Path:
    return cache_dir / "raw" / f"live-{category}.csv"


def fetch_live(category: str, *, cache_dir: Path) -> Path:
    """Scarica lo snapshot giornaliero 'live' (anagrafica o prezzi) mantenendolo in cache.

    Il MIMIT pubblica ogni mattina l'estrazione del giorno precedente sovrascrivendo quella
    prima: la copia in cache si riusa solo se contiene già quell'estrazione. Una scadenza a
    tempo riuserebbe una copia scaricata prima della pubblicazione e la giornata andrebbe
    persa.
    """
    url = config.LIVE_ANAGRAFICA if category == "anagrafica_impianti_attivi" else config.LIVE_PREZZI
    dest = live_path(category, cache_dir)
    if dest.exists():
        day = parsing.file_extraction_date(dest)
        if day and day >= date.today() - timedelta(days=1):
            return dest
    # download_file non sovrascrive un file esistente: si scarica su un file nuovo e lo si
    # sostituisce, senza riprendere eventuali download parziali di un giorno precedente
    fresh = dest.with_name(dest.name + ".new")
    for stale in (fresh, fresh.with_name(fresh.name + ".part")):
        stale.unlink(missing_ok=True)
    try:
        util.download_file(url, fresh)
    except Exception as exc:  # noqa: BLE001
        if not dest.exists():
            raise
        util.log(f"{dest.name}: aggiornamento non riuscito ({exc}), uso la copia in cache", "warn")
        return dest
    fresh.replace(dest)
    return dest


# --------------------------------------------------------------------------------------
# Download completo "una tantum" (gli archivi storici non cambiano più)
# --------------------------------------------------------------------------------------

MANIFEST_NAME = "manifest.json"


def load_manifest(cache_dir: Path) -> dict[str, Any]:
    path = cache_dir / MANIFEST_NAME
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_manifest(cache_dir: Path, manifest: dict[str, Any]) -> None:
    path = cache_dir / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def sync_archives(
    quarters: list[Quarter],
    *,
    cache_dir: Path,
    urls: dict[str, dict[str, str]] | None = None,
    categories: tuple[str, ...] = config.CATEGORIES,
    jobs: int = 3,
    quiet: bool = False,
) -> dict[str, int]:
    """Scarica (una volta sola) gli archivi completi dei trimestri indicati.

    Gli archivi pubblicati sono immutabili: una volta scaricati e registrati nel manifest
    non vengono più né riscaricati né ricontrollati. Il download è ripartibile (resume).
    """
    import concurrent.futures

    manifest = load_manifest(cache_dir)
    todo: list[tuple[Quarter, str, Path, str]] = []
    skipped = 0
    for quarter in quarters:
        entry = manifest.setdefault(quarter.key, {})
        for category in categories:
            dest = cache_dir / "raw" / f"{category}-{quarter.archive_name}"
            recorded = entry.get(category) or {}
            if dest.exists() and recorded.get("bytes") and dest.stat().st_size == recorded["bytes"]:
                skipped += 1
                continue
            url = ((urls or {}).get(quarter.key, {}) or {}).get(category) or archive_url(quarter, category)
            todo.append((quarter, category, dest, url))
    if not quiet:
        util.log(
            f"archivi da scaricare: {len(todo)} (già presenti e verificati: {skipped})",
            "step" if todo else "ok",
        )
    if not todo:
        return {"downloaded": 0, "skipped": skipped, "bytes": 0}

    total_bytes = 0
    downloaded = 0
    lock = __import__("threading").Lock()

    def worker(task: tuple[Quarter, str, Path, str]) -> tuple[str, tuple[str, str, int]] | None:
        quarter, category, dest, url = task
        try:
            util.download_file(url, dest, quiet=True)
        except Exception as exc:  # noqa: BLE001
            return f"{quarter.key} {category}: fallito ({exc})"
        size = dest.stat().st_size
        with lock:
            manifest.setdefault(quarter.key, {})[category] = {
                "file": dest.name,
                "bytes": size,
                "downloaded": time.strftime("%Y-%m-%dT%H:%M"),
                "url": url,
            }
        if not quiet:
            util.log(f"{quarter.key} · {category}: {util.human_bytes(size)}", "debug")
        return None if quiet else f"{quarter.key} · {category}: scaricato ({util.human_bytes(size)})"

    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for message in pool.map(worker, todo):
            if message and "fallito" in message:
                errors.append(message)
                util.log(message, "warn")
            elif message:
                downloaded += 1
                log_progress = message
                util.log(log_progress, "ok")
    for quarter in quarters:
        for category in categories:
            info = manifest.get(quarter.key, {}).get(category)
            if info:
                total_bytes += int(info.get("bytes", 0))
    save_manifest(cache_dir, manifest)
    if errors:
        util.log(f"{len(errors)} archivi non scaricati; riprova con lo stesso comando", "warn")
    return {"downloaded": downloaded, "skipped": skipped, "bytes": total_bytes, "errors": len(errors)}
