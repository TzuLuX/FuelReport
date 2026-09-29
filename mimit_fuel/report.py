"""Generazione del report HTML: template, dati incorporati e librerie.

Il report è un singolo file HTML autoconsistente: i dati (JSON) sono incorporati
compresso in base64 e le librerie (Leaflet, Chart.js) vengono incorporate nel file così
che il report funzioni anche offline. Fanno eccezione i tile della mappa, che sono
scaricati da OpenStreetMap al momento della visualizzazione.
"""

from __future__ import annotations

import base64
import gzip
import json
import sys
import urllib.parse
from html import escape
from pathlib import Path
from typing import Any

from . import config, util

TEMPLATE_DIR = Path(__file__).with_name("templates")


def _read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _vendor(name: str, url: str, cache_dir: Path) -> str | None:
    """Scarica una libreria (una sola volta) e ne restituisce il contenuto."""
    path = cache_dir / "vendor" / name
    if path.exists() and path.stat().st_size > 0:
        return path.read_text(encoding="utf-8", errors="replace")
    try:
        content = util.http_get(url, timeout=120, retries=2).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        util.log(f"libreria {name} non scaricabile ({exc}): uso il CDN", "warn")
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def _lib_tag(name: str, cache_dir: Path) -> str:
    """Libreria incorporata nella pagina; se non è scaricabile, riferimento al CDN."""
    url = config.VENDOR_LIBS[name]
    content = _vendor(name, url, cache_dir)
    if name.endswith(".css"):
        return f"<style>\n{content}\n</style>" if content else f'<link rel="stylesheet" href="{url}">'
    return f"<script>\n{_html_safe(content)}\n</script>" if content else f'<script src="{url}"></script>'


def _encode(payload: Any, *, compress: bool = True) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if compress:
        text = gzip.compress(text, compresslevel=9)
    return base64.b64encode(text).decode("ascii")


def _html_safe(text: str) -> str:
    return text.replace("</script", "<\\/script").replace("<!--", "<\\!--")


#: caratteri base64 per pezzo di blocco dati: ogni pezzo aggiorna la barra di avanzamento
CHUNK_CHARS = 256 * 1024


def _data_tags(blocks: dict[str, str]) -> dict[str, str]:
    """Spezza i blocchi di dati (in ordine di pagina) e segnala l'avanzamento del caricamento.

    Il parser HTML esegue gli script man mano che la pagina arriva: dopo ogni pezzo
    ``__loaded(frazione)`` aggiorna la barra di avanzamento e, alla fine di un blocco,
    ``__loaded(frazione, nome)`` segnala all'app che quel blocco è disponibile.
    """
    total = sum(len(data) for data in blocks.values()) or 1
    done = 0
    tags: dict[str, str] = {}
    for name, data in blocks.items():
        chunks = [data[i:i + CHUNK_CHARS] for i in range(0, len(data), CHUNK_CHARS)] or [""]
        parts = []
        for index, chunk in enumerate(chunks):
            done += len(chunk)
            block = f", {json.dumps(name)}" if index == len(chunks) - 1 else ""
            parts.append(f'<script type="text/plain" data-block="{name}">{chunk}</script>')
            parts.append(f"<script>__loaded({done / total:.4f}{block})</script>")
        tags[name] = "\n".join(parts)
    return tags


def _tile_providers(custom: str | None = None) -> list[dict[str, str]]:
    """Elenco dei provider di mappe di base, con l'eventuale URL personalizzato in testa."""
    providers = [
        {"name": provider["name"], "url": provider["url"], "attribution": provider["attribution"]}
        for provider in config.TILE_PROVIDERS
    ]
    if custom:
        host = urllib.parse.urlsplit(custom).hostname or custom
        providers.insert(0, {
            "name": "Personalizzato",
            "url": custom,
            # i confini disegnati sopra lo sfondo restano dati OpenStreetMap
            "attribution": f"tile: {escape(host)} · confini &copy; "
            '<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        })
    return providers


def build_footer(payload: dict[str, Any]) -> str:
    """Contenuto informativo di fondo pagina: fonti, copertura, note metodologiche."""
    meta = payload.get("meta", {})
    period = meta.get("period", ["?", "?"])
    rows = meta.get("rows", 0)
    unmapped = meta.get("unmapped", 0)
    share = (unmapped / rows * 100) if rows else 0
    granularity = meta.get("granularity", {})
    notes = meta.get("notes", [])
    items = [
        f"Archivi trimestrali MIMIT elaborati: <b>{', '.join(meta.get('archives', []))}</b> "
        f"({len(meta.get('archives', []))} trimestri, {period[0]} → {period[1]}).",
        f"Righe di prezzo elaborate: <b>{rows:,}</b>".replace(",", ".") + ".",
        f"Righe non attribuibili a un comune/provincia (impianti non presenti nelle anagrafiche): "
        f"{unmapped:,} ({share:.2f}%)".replace(",", ".") + ".",
        "Granularità delle serie: nazionale e regionale "
        f"{granularity.get('it', 'giornaliere')}, provinciale {granularity.get('provinces', 'giornaliere')}, "
        f"comunale {granularity.get('comuni', 'mensili')}.",
        "Le serie sono medie <i>pesate sul numero di impianti</i>; i prezzi sono quelli comunicati dagli "
        "esercenti e rilevati dal MIMIT alle ore 8.",
        f"Report generato il {meta.get('generated', '')} con "
        f"<code>{' '.join(sys.argv)[:180]}</code>.",
    ]
    items.extend(notes)
    live_day = meta.get("live_day")
    archive_end = meta.get("archive_end")
    if live_day and archive_end and live_day > archive_end:
        items.append(
            f"L'ultima rilevazione ({live_day}) proviene dal <b>feed giornaliero</b> del MIMIT: gli archivi "
            f"trimestrali sono pubblicati con alcuni mesi di ritardo, quindi dopo il {archive_end} le serie "
            "contengono solo i giorni del feed raccolti dal tool. Nel grafico dell'andamento il periodo non "
            "ancora pubblicato è una fascia grigia e le rilevazioni del feed sono punti con il proprio valore. "
            "Eseguendo periodicamente il tool le rilevazioni giornaliere vengono conservate e la serie recente "
            "si completa."
        )
    html = [
        "<p><b>Note metodologiche</b></p><ul>",
        "".join(f"<li>{item}</li>" for item in items),
        "</ul>",
        '<p><b>Fonti e licenze</b></p><ul>',
        f'<li>Prezzi e anagrafica impianti: <a href="{config.SOURCE_PAGE}">MIMIT — Carburanti, archivio storico '
        "dei prezzi praticati e dell'anagrafica degli impianti</a> (dati open data, fonte "
        "<a href=\"https://opendatacarburanti.mise.gov.it/\">opendatacarburanti.mise.gov.it</a>).</li>",
        "<li>Confini amministrativi e mappa: © "
        '<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors (ODbL), '
        "geometrie da Overpass API e Nominatim.</li>",
        "<li>Le mappe di base sono servite da più server OpenStreetMap (OpenStreetMap.org, FOSSGIS, "
        "stile Humanitarian): il report verifica all'avvio quale sia utilizzabile e ripiega automaticamente "
        "sugli altri, perché il server principale blocca le richieste che arrivano da pagine aperte con "
        "<code>file://</code>. Lo sfondo si può cambiare dal menu della mappa.</li>",
        "<li>Le elaborazioni (medie, variazioni, classifiche) sono calcolate da questo tool a partire dai "
        "dati grezzi: possono differire dalle statistiche ufficiali del Ministero.</li>",
        "</ul>",
    ]
    return "\n".join(html)


def render(
    payload: dict[str, Any],
    *,
    data_dir: Path,
    quiet: bool = False,
    compress: bool = True,
    plain: bool = False,
    tiles: str | None = None,
) -> tuple[str, dict[str, int]]:
    """Costruisce il file HTML a partire dal payload delle serie storiche."""
    if plain:
        compress = False
    cache_dir = data_dir / "cache"
    core = {
        "meta": payload["meta"],
        "dates": payload["dates"],
        "italy": payload["italy"],
        # dei conteggi degli impianti il report usa solo l'ultimo valore
        "italy_counts": {combo: counts[-1:] for combo, counts in payload["italy_counts"].items()},
        "italy_axes": payload.get("italy_axes", {}),
        "regions": payload["regions"],
        "region_counts": {
            region: {combo: counts[-1:] for combo, counts in combos.items()}
            for region, combos in payload["region_counts"].items()
        },
        "dispersion": payload["dispersion"],
    }
    provinces = {
        "provinces": payload["provinces"],
        "provinces_last": payload["provinces_last"],
    }
    comuni = {
        "keys": payload["comuni_keys"],
        "meta": payload["comuni_meta"],
        "monthly": payload["comuni_monthly"],
        "last": payload["comuni_last"],
    }
    cfg = {
        "tileProviders": _tile_providers(tiles),
        "period": payload["meta"]["period"],
        "archiveEnd": payload["meta"].get("archive_end"),
        "liveDay": payload["meta"].get("live_day"),
        "fuels": payload["meta"]["fuels"],
        "granularity": payload["meta"]["granularity"],
        "regionNames": {slug: name for slug, name in config.REGION_NAMES.items() if slug in payload["regions"]},
        "provinceNames": {code: config.PROVINCES.get(code, code) for code in payload["provinces"]},
        "provinceToRegion": config.PROVINCE_TO_REGION,
        "boundaries": "boundaries" in payload,
        "compress": compress,
    }

    # nell'ordine della pagina: l'app parte dopo core e confini, il resto arriva in sottofondo
    blocks = {
        "core": _encode(core, compress=compress),
        "boundaries": _encode(payload.get("boundaries") or {}, compress=compress),
        "provinces": _encode(provinces, compress=compress),
        "comuni": _encode(comuni, compress=compress),
        "stations": _encode(payload.get("stations") or {}, compress=compress),
    }
    stats = {f"__DATA_{name.upper()}__": len(value) for name, value in blocks.items()}
    data_size = util.human_bytes(sum(len(value) for value in blocks.values())).replace(".", ",")

    html = _read_template("report.html")
    replacements = {
        "__TITLE__": "Prezzi carburanti in Italia — report open data MIMIT",
        "__SOURCE_URL__": config.SOURCE_PAGE,
        "__DATA_SIZE__": data_size,
        "__LEAFLET_CSS__": _lib_tag("leaflet.css", cache_dir),
        "__LEAFLET_JS__": _lib_tag("leaflet.js", cache_dir),
        "__CHART_JS__": _lib_tag("chart.js", cache_dir),
        "__HAMMER_JS__": _lib_tag("hammer.js", cache_dir),
        "__ZOOM_JS__": _lib_tag("chartjs-plugin-zoom.js", cache_dir),
        "__REPORT_CSS__": _read_template("report.css"),
        "__APP_JS__": _html_safe(_read_template("report.js")),
        "__CFG__": json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/"),
        "__META_HTML__": build_footer(payload),
    }
    # i dati per ultimi: il base64 non contiene segnaposto da sostituire
    replacements.update({f"__DATA_{name.upper()}__": tags for name, tags in _data_tags(blocks).items()})
    for key, value in replacements.items():
        html = html.replace(key, value)

    stats["payload_bytes"] = sum(len(value) for name, value in blocks.items() if name != "boundaries")
    if not quiet:
        util.log(
            "payload incorporato: "
            + ", ".join(f"{name} {util.human_bytes(len(value))}" for name, value in blocks.items()),
            "debug",
        )
    return html, stats
