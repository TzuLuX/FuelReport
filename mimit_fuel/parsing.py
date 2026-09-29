"""Lettura dei file MIMIT: anagrafica impianti, prezzi giornalieri, normalizzazione.

I file pubblicati hanno subito nel tempo piccoli cambi di formato (separatore ``;`` fino
al 2020, ``|`` dal 2021; riga di intestazione "Estrazione del ..." presente solo dopo il
2016; nomi di colonna con maiuscole/minuscole diverse): qui vengono normalizzati in un
formato unico.
"""

from __future__ import annotations

import re
import tarfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import IO, Iterator

from . import config, util

#: indice nel record compatto di una stazione
ANA_GESTORE, ANA_BANDIERA, ANA_TIPO, ANA_NOME, ANA_INDIRIZZO, ANA_COMUNE, ANA_PROV, ANA_LAT, ANA_LON = range(9)

_DATE_IN_NAME = re.compile(r"(\d{4})(\d{2})(\d{2})")

ANA_ALIASES = {
    "id": ("idImpianto", "id_impianto", "id impianto", "id"),
    "gestore": ("Gestore",),
    "bandiera": ("Bandiera",),
    "tipo": ("Tipo Impianto",),
    "nome": ("Nome Impianto",),
    "indirizzo": ("Indirizzo",),
    "comune": ("Comune",),
    "provincia": ("Provincia",),
    "lat": ("Latitudine",),
    "lon": ("Longitudine",),
}

PRICE_ALIASES = {
    "id": ("idImpianto", "id_impianto", "id impianto"),
    "carburante": ("descCarburante", "desc_carburante"),
    "prezzo": ("prezzo",),
    "self": ("isSelf", "is_self", "self"),
}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class SeeklessReader:
    """Proxy che rende avvolgibile in ``TextIOWrapper`` uno stream tar non seekable.

    In modalità streaming gli oggetti restituiti da ``tarfile`` non implementano
    ``seekable()``: questo wrapper espone l'interfaccia minima richiesta da
    ``io.TextIOWrapper`` mantenendo la lettura sequenziale (veloce e a memoria costante).
    """

    __slots__ = ("_obj",)

    def __init__(self, obj):
        self._obj = obj

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._obj.read(size)

    def read1(self, size: int = -1) -> bytes:
        reader = getattr(self._obj, "read1", None)
        return reader(size) if reader else self._obj.read(size)

    def readinto(self, buffer) -> int:
        data = self._obj.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def flush(self) -> None:
        """No-op: il buffer sottostante non è scrivibile."""

    def fileno(self) -> int:
        raise OSError("stream non seekable")

    @property
    def closed(self) -> bool:
        return False

    def close(self) -> None:  # noqa: D401 - lo stream resta di proprietà di tarfile
        """Non chiude lo stream sottostante (di proprietà di ``tarfile``)."""
        return None


def _header_map(fields: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    lookup = {_key(f): i for i, f in enumerate(fields)}
    resolved: dict[str, int] = {}
    for name, options in aliases.items():
        for option in options:
            if _key(option) in lookup:
                resolved[name] = lookup[_key(option)]
                break
    return resolved


def read_header(fh: IO[bytes]) -> tuple[str, list[str]]:
    """Legge le prime righe fino all'intestazione vera e propria del CSV.

    Restituisce ``(delimitatore, campi)``; la riga "Estrazione del ..." (presente negli
    archivi recenti) viene scartata insieme alle righe vuote iniziali.
    """
    for _ in range(6):
        raw = fh.readline()
        if not raw:
            break
        line = raw.decode("utf-8-sig", errors="replace").strip()
        if not line:
            continue
        delim = util.sniff_delimiter(line)
        fields = [f.strip() for f in line.split(delim)]
        keys = {_key(f) for f in fields}
        if keys & {"idimpianto", "id", "prezzo", "desccarburante"}:
            return delim, fields
    raise ValueError("intestazione CSV non riconosciuta")


def price_columns(fh: IO[bytes]) -> tuple[str, dict[str, int]]:
    """Legge l'intestazione di un file prezzi e restituisce ``(delimitatore, indici)``."""
    delim, fields = read_header(fh)
    return delim, _header_map(fields, PRICE_ALIASES)


def extraction_date(line: str) -> date | None:
    """Estrae la data dalla riga "Estrazione del YYYY-MM-DD"."""
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", line)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def file_extraction_date(path: Path) -> date | None:
    """Legge la data di estrazione dichiarata nella prima riga di un CSV MIMIT."""
    with open(path, "rb") as fh:
        head = fh.read(200).decode("utf-8-sig", errors="replace")
    for line in head.splitlines():
        if "strazione" in line:
            return extraction_date(line)
    return None


def date_from_filename(name: str) -> date | None:
    match = _DATE_IN_NAME.search(Path(name).name)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


# --------------------------------------------------------------------------------------
# Anagrafica
# --------------------------------------------------------------------------------------


def read_anagrafica_stream(fh: IO[bytes], store: dict[str, list[str]]) -> int:
    """Legge un CSV di anagrafica e aggiorna ``store`` (idImpianto -> record compatto)."""
    try:
        delim, fields = read_header(fh)
    except ValueError:
        return 0
    idx = _header_map(fields, ANA_ALIASES)
    if "id" not in idx:
        return 0
    expected = max(2, len(fields) - 3)
    count = 0
    for raw in fh:
        parts = raw.decode("utf-8-sig", errors="replace").rstrip("\r\n").split(delim)
        if len(parts) < expected:
            continue
        sid = parts[idx["id"]].strip() if idx["id"] < len(parts) else ""
        if not sid or not sid.isdigit():
            continue

        def field(name: str) -> str:
            position = idx.get(name)
            return parts[position].strip() if position is not None and position < len(parts) else ""

        store[sid] = [
            field("gestore"),
            field("bandiera"),
            field("tipo"),
            field("nome"),
            field("indirizzo"),
            field("comune"),
            field("provincia"),
            field("lat"),
            field("lon"),
        ]
        count += 1
    return count


def read_anagrafica_file(path: Path, store: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Legge un singolo CSV di anagrafica (es. lo snapshot "live")."""
    store = store if store is not None else {}
    with open(path, "rb") as fh:
        read_anagrafica_stream(fh, store)
    return store


def read_anagrafica_archive(path: Path, store: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Legge tutti gli snapshot giornalieri di anagrafica contenuti in un archivio tar.gz.

    L'unione degli snapshot copre sia gli impianti chiusi durante il trimestre sia quelli
    attivati successivamente: gli snapshot più recenti prevalgono sui precedenti.
    """
    store = store if store is not None else {}
    members: list[tuple[date, str]] = []
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".csv"):
                continue
            day = date_from_filename(member.name)
            if day is None:
                continue
            members.append((day, member.name))
        for _day, name in sorted(members):
            fh = tar.extractfile(name)
            if fh is not None:
                read_anagrafica_stream(fh, store)
    return store


def iter_price_days(path: Path, must_contain: str = "prezzo") -> Iterator[tuple[date, IO[bytes]]]:
    """Itera i CSV giornalieri di prezzo contenuti in un archivio trimestrale.

    Usa la modalità streaming di ``tarfile`` per non caricare in memoria l'archivio.
    Il chiamante **deve** consumare completamente il file restituito prima di proseguire.
    """
    with tarfile.open(path, "r|gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".csv"):
                continue
            if must_contain and must_contain not in Path(member.name).name:
                continue
            day = date_from_filename(member.name)
            if day is None:
                continue
            fh = tar.extractfile(member)
            if fh is not None:
                yield day, fh


# --------------------------------------------------------------------------------------
# Indice geografico degli impianti
# --------------------------------------------------------------------------------------


def _normalize_comune(value: str) -> str:
    return config.normalize_text(value)


class GeoIndex:
    """Mappa ``idImpianto`` -> (provincia, comune) risolvendo l'anagrafica "sporca"."""

    def __init__(self, stations: dict[str, list[str]]):
        self.stations = stations
        self.provinces: list[str] = []
        self.comuni: list[str] = []
        self.comuni_meta: dict[str, dict[str, object]] = {}
        self.mapping: dict[str, int] = {}
        self.unresolved = 0
        self.without_coords = 0
        self._prov_idx: dict[str, int] = {}
        self._com_idx: dict[str, int] = {}
        self._build()

    # -- costruzione -----------------------------------------------------------------
    def _build(self) -> None:
        rows: list[tuple[str, str, str]] = []  # (id, sigla risolta o "", comune normalizzato)
        votes: dict[str, Counter[str]] = defaultdict(Counter)
        for sid, rec in self.stations.items():
            comune = _normalize_comune(rec[ANA_COMUNE]) or "N/D"
            sigla = config.resolve_provincia(rec[ANA_PROV]) or ""
            if sigla:
                votes[comune][sigla] += 1
            rows.append((sid, sigla, comune))

        for sid, sigla, comune in rows:
            if not sigla:
                counter = votes.get(comune)
                if counter:
                    best, best_n = counter.most_common(1)[0]
                    if best_n / sum(counter.values()) >= 0.6:
                        sigla = best
            if not sigla:
                self.unresolved += 1
                continue

            lat, lon = self._coords(self.stations[sid])
            com_key = f"{sigla}|{comune}"
            if com_key not in self._com_idx:
                self._com_idx[com_key] = len(self.comuni)
                self.comuni.append(com_key)
                self.comuni_meta[com_key] = {
                    "prov": sigla,
                    "nome": comune,
                    "n": 0,
                    "n_coord": 0,
                    "_lat": 0.0,
                    "_lon": 0.0,
                    "lat": None,
                    "lon": None,
                }
            if sigla not in self._prov_idx:
                self._prov_idx[sigla] = len(self.provinces)
                self.provinces.append(sigla)

            meta = self.comuni_meta[com_key]
            meta["n"] = int(meta["n"]) + 1
            if lat is not None and lon is not None:
                meta["n_coord"] = int(meta["n_coord"]) + 1
                meta["_lat"] = float(meta["_lat"]) + lat
                meta["_lon"] = float(meta["_lon"]) + lon
            else:
                self.without_coords += 1
            self.mapping[sid] = (self._prov_idx[sigla] << 20) | self._com_idx[com_key]

        for meta in self.comuni_meta.values():
            n = int(meta.pop("n_coord", 0))
            lat_sum = float(meta.pop("_lat", 0.0))
            lon_sum = float(meta.pop("_lon", 0.0))
            meta["lat"] = round(lat_sum / n, 5) if n else None
            meta["lon"] = round(lon_sum / n, 5) if n else None

    @staticmethod
    def _coords(rec: list[str]) -> tuple[float | None, float | None]:
        try:
            lat = float(rec[ANA_LAT].replace(",", "."))
            lon = float(rec[ANA_LON].replace(",", "."))
        except (ValueError, IndexError, AttributeError):
            return None, None
        if not (35.0 <= lat <= 48.5 and 6.0 <= lon <= 19.0):
            return None, None
        return lat, lon

    # -- accesso ---------------------------------------------------------------------
    def coverage(self, ids: set[str]) -> float:
        if not ids:
            return 1.0
        known = sum(1 for sid in ids if sid in self.mapping)
        return known / len(ids)

    def province_names(self) -> list[str]:
        return list(self.provinces)

    def to_dict(self) -> dict[str, object]:
        return {
            "mapping": self.mapping,
            "provinces": self.provinces,
            "comuni": self.comuni,
            "comuni_meta": self.comuni_meta,
            "stations": self.stations,
            "unresolved": self.unresolved,
            "without_coords": self.without_coords,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GeoIndex":
        obj = cls.__new__(cls)
        obj.stations = payload.get("stations", {})  # type: ignore[assignment]
        obj.mapping = payload["mapping"]  # type: ignore[assignment]
        obj.provinces = payload["provinces"]  # type: ignore[assignment]
        obj.comuni = payload["comuni"]  # type: ignore[assignment]
        obj.comuni_meta = payload["comuni_meta"]  # type: ignore[assignment]
        obj.unresolved = int(payload.get("unresolved", 0))
        obj.without_coords = int(payload.get("without_coords", 0))
        obj._prov_idx = {p: i for i, p in enumerate(obj.provinces)}
        obj._com_idx = {c: i for i, c in enumerate(obj.comuni)}
        return obj
