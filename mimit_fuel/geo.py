"""Confini amministrativi da OpenStreetMap per la mappa interattiva.

La mappa usa:

* i tile di OpenStreetMap per il disegno di base (``tile.openstreetmap.org``);
* i confini di regioni (``admin_level=4``) e province (``admin_level=6``) recuperati da
  OpenStreetMap. Gli identificativi delle relazioni sono presi da **Overpass API** (tag
  ``ISO3166-2``, che coincide con le sigle usate dal MIMIT); se Overpass non è
  raggiungibile si ripiega sulla ricerca strutturata di **Nominatim** usando le tabelle
  geografiche interne. In entrambi i casi le geometrie (già semplificate) arrivano da
  Nominatim con ``polygon_geojson=1``.

Tutto viene messo in cache su disco: le generazioni successive del report non richiedono
nuovamente la rete per la parte cartografica.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from . import config, util

BOUNDARY_CACHE_VERSION = 3
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"


# --------------------------------------------------------------------------------------
# Relazioni amministrative
# --------------------------------------------------------------------------------------


def _overpass_post(endpoint: str, query: str) -> bytes:
    data = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "User-Agent": config.USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        return response.read()


def _relations_via_overpass(quiet: bool = False) -> dict[str, dict[str, Any]]:
    """``{codice: {id, nome, livello}}`` per regioni e province italiane, da Overpass."""
    query = (
        "[out:json][timeout:180];"
        'rel["ISO3166-2"~"^IT-"]["admin_level"~"^(4|6)$"];'
        "out tags;"
    )
    last_error: Exception | None = None
    for endpoint in config.OVERPASS_ENDPOINTS:
        for attempt in range(2):
            try:
                payload = json.loads(_overpass_post(endpoint, query))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(2 + attempt * 5)
                continue
            relations: dict[str, dict[str, Any]] = {}
            for element in payload.get("elements", []):
                tags = element.get("tags", {})
                iso = tags.get("ISO3166-2", "")
                if not iso.startswith("IT-"):
                    continue
                relations[iso[3:]] = {
                    "id": element["id"],
                    "name": tags.get("name", iso[3:]),
                    "level": int(tags.get("admin_level", 0)),
                }
            if relations:
                if not quiet:
                    util.log(f"Overpass: {len(relations)} relazioni amministrative italiane", "ok")
                return relations
    raise RuntimeError(f"Overpass non raggiungibile ({last_error})")


def _nominatim_search(params: dict[str, str], *, threshold: float = 0.001) -> dict[str, Any] | None:
    """Una ricerca Nominatim che restituisce la prima relazione amministrativa trovata."""
    query = dict(params)
    query.update(
        {
            "format": "geojson",
            "polygon_geojson": "1",
            "polygon_threshold": str(threshold),
            "limit": "3",
            "countrycodes": "it",
        }
    )
    url = f"{NOMINATIM_SEARCH}?{urllib.parse.urlencode(query)}"
    try:
        payload = json.loads(util.http_get(url, timeout=90, retries=2))
    except Exception as exc:  # noqa: BLE001
        util.log(f"Nominatim non raggiungibile per {params}: {exc}", "warn")
        return None
    finally:
        time.sleep(1.1)  # usage policy: max 1 richiesta al secondo
    for feature in payload.get("features", []):
        properties = feature.get("properties", {})
        if properties.get("osm_type") == "relation" and properties.get("type") == "administrative":
            return feature
    return None


def _relations_via_nominatim(quiet: bool = False) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Ripiego: risolve le relazioni (e le geometrie) per nome tramite Nominatim."""
    if not quiet:
        util.log(
            "Overpass non disponibile: risolvo i confini con Nominatim "
            "(una richiesta al secondo, ~2 minuti la prima volta)",
            "step",
        )
    relations: dict[str, dict[str, Any]] = {}
    geometries: dict[int, dict[str, Any]] = {}
    total = len(config.REGIONS) + len(config.PROVINCES)
    done = 0
    for region_code, (slug, name, _provinces) in config.REGIONS.items():
        feature = _nominatim_search({"state": name, "country": "Italia"})
        done += 1
        if feature:
            osm_id = int(feature["properties"]["osm_id"])
            relations[region_code[3:]] = {"id": osm_id, "name": name, "level": 4}
            geometries[osm_id] = feature
        else:
            util.log(f"confine non trovato per la regione {name}", "warn")
        if not quiet and done % 25 == 0:
            util.log(f"Nominatim: {done}/{total} aree risolte", "debug")
    for sigla, nome in config.PROVINCES.items():
        region_slug = config.PROVINCE_TO_REGION.get(sigla)
        params = {"county": nome, "country": "Italia"}
        if region_slug:
            params["state"] = config.REGION_NAMES.get(region_slug, "")
        feature = _nominatim_search(params)
        done += 1
        if not feature and sigla == "AO":  # la Valle d'Aosta non ha una provincia separata
            feature = geometries.get(relations.get("23", {}).get("id"))
        if feature:
            osm_id = int(feature["properties"]["osm_id"])
            relations[sigla] = {"id": osm_id, "name": nome, "level": 6}
            geometries.setdefault(osm_id, feature)
        else:
            util.log(f"confine non trovato per la provincia {nome} ({sigla})", "warn")
        if not quiet and done % 25 == 0:
            util.log(f"Nominatim: {done}/{total} aree risolte", "debug")
    if not relations:
        raise RuntimeError("nessuna relazione amministrativa risolta")
    return relations, geometries


def _geometries_via_lookup(
    relation_ids: Iterable[int], *, batch: int = 35, threshold: float = 0.001, quiet: bool = False
) -> dict[int, dict[str, Any]]:
    """Scarica le geometrie delle relazioni indicate a lotti (endpoint ``/lookup``)."""
    ids = list(relation_ids)
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), batch):
        chunk = ids[start : start + batch]
        query = urllib.parse.urlencode(
            {
                "osm_ids": ",".join(f"R{i}" for i in chunk),
                "format": "geojson",
                "polygon_geojson": "1",
                "polygon_threshold": str(threshold),
            }
        )
        payload = json.loads(util.http_get(f"{config.NOMINATIM_ENDPOINT}?{query}", timeout=120, retries=3))
        for feature in payload.get("features", []):
            osm_id = feature.get("properties", {}).get("osm_id")
            if osm_id is not None:
                out[int(osm_id)] = feature
        if not quiet:
            util.log(f"Nominatim: {len(out)}/{len(ids)} geometrie scaricate", "debug")
        time.sleep(1.1)
    return out


# --------------------------------------------------------------------------------------
# Semplificazione geometrica
# --------------------------------------------------------------------------------------


def _rdp(points: list[list[float]], epsilon: float) -> list[list[float]]:
    """Semplificazione Douglas-Peucker (iterativa, senza ricorsione)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        x1, y1 = points[first]
        x2, y2 = points[last]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        best_index, best_distance = -1, epsilon
        for index in range(first + 1, last):
            px, py = points[index]
            distance = math.hypot(px - x1, py - y1) if norm == 0 else abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
            if distance > best_distance:
                best_index, best_distance = index, distance
        if best_index > 0:
            keep[best_index] = True
            stack.append((first, best_index))
            stack.append((best_index, last))
    return [point for point, flag in zip(points, keep) if flag]


def _ring_area(ring: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(ring) - 1):
        area += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(area) / 2.0


def simplify_geometry(geometry: dict[str, Any], epsilon: float = 0.002, min_extent: float = 0.03) -> dict[str, Any]:
    """Semplifica una geometria GeoJSON preservando i poligoni significativi."""

    def simplify_ring(ring: list[list[float]]) -> list[list[float]]:
        rounded: list[list[float]] = []
        for point in ring:
            candidate = [round(point[0], 4), round(point[1], 4)]
            if not rounded or candidate != rounded[-1]:
                rounded.append(candidate)
        if len(rounded) < 4:
            return rounded
        if rounded[0] != rounded[-1]:
            rounded.append(rounded[0])
        simplified = _rdp(rounded, epsilon)
        if simplified and simplified[0] != simplified[-1]:
            simplified.append(simplified[0])
        return simplified

    def simplify_polygon(rings: list[list[list[float]]]) -> list[list[list[float]]]:
        if not rings:
            return rings
        processed = [simplify_ring(ring) for ring in rings]
        keep = [processed[0]]
        for ring in processed[1:]:
            xs = [point[0] for point in ring]
            ys = [point[1] for point in ring]
            if len(ring) >= 4 and max(max(xs) - min(xs), max(ys) - min(ys)) >= min_extent:
                keep.append(ring)
        return keep

    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": simplify_polygon(geometry["coordinates"])}
    if geometry["type"] == "MultiPolygon":
        polygons = [simplify_polygon(poly) for poly in geometry["coordinates"]]
        polygons = [poly for poly in polygons if poly and len(poly[0]) >= 4]
        polygons.sort(key=lambda poly: _ring_area(poly[0]), reverse=True)
        return {"type": "MultiPolygon", "coordinates": polygons}
    return geometry


# --------------------------------------------------------------------------------------
# Costruzione e cache
# --------------------------------------------------------------------------------------


def build_boundaries(*, quiet: bool = False) -> dict[str, Any]:
    """Scarica e prepara i confini di regioni e province italiane."""
    geometries: dict[int, dict[str, Any]] = {}
    try:
        relations = _relations_via_overpass(quiet=quiet)
    except Exception as exc:  # noqa: BLE001
        util.log(f"Overpass non disponibile ({exc})", "warn")
        relations, geometries = _relations_via_nominatim(quiet=quiet)

    if not geometries:
        wanted = [rel["id"] for rel in relations.values()]
        geometries = _geometries_via_lookup(wanted, quiet=quiet)

    features: list[dict[str, Any]] = []
    for code, rel in sorted(relations.items()):
        if rel["level"] == 4:
            feature = geometries.get(rel["id"])
            if not feature:
                continue
            slug = config.REGIONS.get(f"IT-{code}", (code.lower(), rel["name"], ()))[0]
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "code": code,
                        "slug": slug,
                        "name": rel["name"],
                        "level": 4,
                        "osm": f"https://www.openstreetmap.org/relation/{rel['id']}",
                    },
                    "geometry": simplify_geometry(feature["geometry"], epsilon=0.003, min_extent=0.05),
                }
            )
    for code, rel in sorted(relations.items()):
        if rel["level"] != 6:
            continue
        feature = geometries.get(rel["id"])
        if not feature:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "code": code,
                    "name": rel["name"],
                    "level": 6,
                    "region": config.PROVINCE_TO_REGION.get(code),
                    "osm": f"https://www.openstreetmap.org/relation/{rel['id']}",
                },
                "geometry": simplify_geometry(feature["geometry"], epsilon=0.002, min_extent=0.03),
            }
        )
    if not features:
        raise RuntimeError("nessuna geometria amministrativa recuperata")
    return {
        "version": BOUNDARY_CACHE_VERSION,
        "fetched": time.strftime("%Y-%m-%d"),
        "source": "OpenStreetMap (Overpass API + Nominatim), ODbL",
        "attribution": '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        "features": features,
    }


def load_boundaries(cache_dir: Path, *, refresh: bool = False, quiet: bool = False) -> dict[str, Any] | None:
    """Restituisce i confini, rigenerandoli solo se necessario."""
    path = cache_dir / "geo" / "boundaries.json.gz"
    if path.exists() and not refresh:
        try:
            payload = util.read_json_gz(path)
            if payload.get("version") == BOUNDARY_CACHE_VERSION:
                return payload
        except Exception:  # noqa: BLE001
            pass
    try:
        payload = build_boundaries(quiet=quiet)
    except Exception as exc:  # noqa: BLE001
        util.log(f"confini OSM non disponibili: {exc}", "warn")
        return None
    util.write_json_gz(path, payload, level=9)
    return payload
