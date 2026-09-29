"""Utility trasversali: logging, download HTTP con cache e resume, I/O JSON compresso."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from . import config

_VERBOSE = True
_T0 = time.time()


def set_verbose(value: bool) -> None:
    global _VERBOSE
    _VERBOSE = value


def log(message: str, level: str = "info") -> None:
    """Stampa un messaggio con un prefisso leggibile."""
    if not _VERBOSE and level == "debug":
        return
    symbols = {"info": "·", "step": "▸", "ok": "✓", "warn": "!", "err": "✗", "debug": " "}
    stamp = f"[{time.time() - _T0:6.1f}s]"
    stream = sys.stderr if level in {"warn", "err"} else sys.stdout
    print(f"{stamp} {symbols.get(level, '·')} {message}", file=stream, flush=True)


def human_bytes(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} GB"


def _request(url: str, *, headers: dict[str, str] | None = None, timeout: float = 90):
    base_headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if headers:
        base_headers.update(headers)
    return urllib.request.Request(url, headers=base_headers)


def http_get(
    url: str,
    *,
    timeout: float = 90,
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Scarica una URL piccola in memoria, con retry e backoff."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(_request(url, headers=headers, timeout=timeout), timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"impossibile scaricare {url}: {last_error}")


def http_status(url: str, *, timeout: float = 30) -> int:
    """Restituisce il codice HTTP di una URL (HEAD, con fallback GET)."""
    try:
        req = _request(url, headers={"Range": "bytes=0-0"}, timeout=timeout)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def download_file(
    url: str,
    dest: Path,
    *,
    timeout: float = 300,
    chunk: int = 1 << 16,
    retries: int = 3,
    quiet: bool = False,
) -> Path:
    """Scarica un file (anche grande) su disco, con supporto al resume e verifica dimensione."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, retries + 1):
        start = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={start}-"} if start else {}
        try:
            req = _request(url, headers=headers, timeout=timeout)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = resp.headers.get("Content-Length")
                total = start + int(total) if total else None
                mode = "ab" if start and resp.status == 206 else "wb"
                if mode == "wb":
                    start = 0
                downloaded = start
                last_report = 0.0
                with open(part, mode) as fh:
                    while True:
                        block = resp.read(chunk)
                        if not block:
                            break
                        fh.write(block)
                        downloaded += len(block)
                        if not quiet and time.time() - last_report > 5:
                            last_report = time.time()
                            pct = f" {100 * downloaded / total:5.1f}%" if total else ""
                            log(
                                f"  download {dest.name}: {human_bytes(downloaded)}"
                                f"{'/' + human_bytes(total) if total else ''}{pct}",
                                "debug",
                            )
            size = part.stat().st_size
            if total and size < total:
                raise RuntimeError(f"download incompleto ({size}/{total} byte)")
            shutil.move(str(part), str(dest))
            if not quiet:
                log(f"scarica {dest.name} ({human_bytes(dest.stat().st_size)})", "ok")
            return dest
        except Exception as exc:  # noqa: BLE001 - retry su qualunque errore di rete
            if attempt >= retries:
                raise RuntimeError(f"download fallito per {url}: {exc}") from exc
            log(f"errore su {url} ({exc}); nuovo tentativo {attempt + 1}/{retries}", "warn")
            time.sleep(min(2 ** attempt, 20))
    return dest


def read_json_gz(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def write_json_gz(path: Path, payload: Any, level: int = 6) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    else:
        text = str(payload)
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=level) as fh:
        fh.write(text)
    tmp.replace(path)
    return len(text)


def iter_file_lines(fh, encoding: str = "utf-8-sig") -> Iterator[str]:
    """Itera le righe di un file binario decodificando al volo."""
    for raw in fh:
        yield raw.decode(encoding, errors="replace").rstrip("\r\n")


def sniff_delimiter(header: str) -> str:
    """Riconosce il separatore usato dal CSV (``;`` fino al 2020, ``|`` dal 2021)."""
    counts = {sep: header.count(sep) for sep in ("|", ";", ",")}
    sep = max(counts, key=counts.get)
    return sep if counts[sep] else ";"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def disk_usage(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total
