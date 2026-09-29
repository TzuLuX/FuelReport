/* Report prezzi carburanti MIMIT — drilldown geografico, grafici e tabelle.
 * I dati sono incorporati nella pagina (JSON compresso, a pezzi) e decodificati su richiesta:
 * l'app parte appena arrivano i dati nazionali, province, comuni e impianti arrivano dopo.
 */
'use strict';

const CFG = window.__REPORT_CFG__ || {};
const state = {
  level: 'it',
  region: null,
  province: null,
  comune: null,
  fuel: null,
  mode: 0,        // 0 = servito, 1 = self, 2 = entrambi
  metric: 'mean',
  period: 'tutto',
};
const defaults = { fuel: null, period: 'tutto' };

const P = { core: null, provinces: null, comuni: null, stations: null, boundaries: null };
let map, overlay, tileLayer;
const charts = {};

const nf = {
  price: new Intl.NumberFormat('it-IT', { minimumFractionDigits: 3, maximumFractionDigits: 3 }),
  pct: new Intl.NumberFormat('it-IT', { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' }),
  cents: new Intl.NumberFormat('it-IT', { minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: 'exceptZero' }),
  int: new Intl.NumberFormat('it-IT'),
  dec1: new Intl.NumberFormat('it-IT', { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
};

// --------------------------------------------------------------------------------------
// Decodifica dei dati incorporati
// --------------------------------------------------------------------------------------

/** Attende che un blocco di dati sia arrivato per intero (segnalato da `__loaded` nella pagina). */
function blockReady(name) {
  if ((window.__blocks || {})[name] || document.readyState !== 'loading') return Promise.resolve();
  return new Promise((resolve) => {
    document.addEventListener('report-block', (event) => { if (event.detail === name) resolve(); });
    document.addEventListener('DOMContentLoaded', () => resolve(), { once: true });
  });
}

async function decodeBlock(name) {
  const nodes = document.querySelectorAll(`script[data-block="${name}"]`);
  const b64 = Array.from(nodes, (node) => node.textContent || '').join('').replace(/\s+/g, '');
  if (!b64) return null;
  const binary = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const isGzip = binary.length > 2 && binary[0] === 0x1f && binary[1] === 0x8b;
  if (!isGzip) return JSON.parse(new TextDecoder().decode(binary));
  if (typeof DecompressionStream === 'undefined') {
    throw new Error('questo browser non supporta DecompressionStream: rigenera il report con --plain');
  }
  const stream = new Blob([binary]).stream().pipeThrough(new DecompressionStream('gzip'));
  return JSON.parse(await new Response(stream).text());
}

const loading = {};

/** Decodifica un blocco una sola volta, anche con richieste concorrenti. */
function ensure(what) {
  if (!loading[what]) loading[what] = loadBlock(what);
  return loading[what];
}

async function loadBlock(what) {
  await blockReady(what);
  const raw = await decodeBlock(what);
  if (what === 'comuni' && raw) {
    const index = Object.create(null);
    raw.keys.forEach((key, i) => { index[key] = i; });
    P.comuni = { keys: raw.keys, meta: raw.meta, monthly: raw.monthly, last: raw.last, index };
  } else {
    P[what] = raw;
  }
  return P[what];
}

// --------------------------------------------------------------------------------------
// Utilità
// --------------------------------------------------------------------------------------

const unit = (fuel) => (fuel === 'Metano' || fuel === 'GNL' ? '€/kg' : '€/l');
const fmtPrice = (v) => (v == null || !isFinite(v) ? '–' : nf.price.format(v));
const fmtDelta = (v) => (v == null || !isFinite(v) ? '–' : nf.pct.format(v) + '%');
const fmtSigned = (v) => (v == null || !isFinite(v) ? '–' : nf.cents.format(v));
const signedClass = (v) => (v == null || !isFinite(v) ? '' : v > 0.001 ? 'num-pos' : v < -0.001 ? 'num-neg' : '');

function scopeCombo() {
  return state.fuel + '|' + (state.mode === 2 ? 0 : state.mode);
}

function modeLabel(mode) {
  return mode === 1 ? 'self service' : 'servito';
}

function granularityKey(level) {
  return level === 'it' ? 'it' : level === 'region' ? 'regions' : level === 'province' ? 'provinces' : 'comuni';
}

function granularityOf(level) {
  return (CFG.granularity || {})[granularityKey(level)] || 'giornaliere';
}

function axisName(level) {
  const axes = (P.core.meta || {}).axes || {};
  return axes[granularityKey(level)] || (level === 'comune' ? 'comuni' : 'daily');
}

function axisDates(level) {
  return ((P.core.dates || {})[axisName(level)]) || [];
}

// --------------------------------------------------------------------------------------
// Periodo di analisi: KPI, mappa, classifiche e tabelle usano solo gli ultimi N mesi
// --------------------------------------------------------------------------------------

// niente «ultimi 3 mesi»: gli archivi MIMIT escono con circa un trimestre di ritardo e quel
// periodo conterrebbe quasi solo le poche rilevazioni del feed giornaliero
const PERIODS = [
  { key: '6m', months: 6, label: 'ultimi 6 mesi' },
  { key: '1a', months: 12, label: 'ultimo anno' },
  { key: '2a', months: 24, label: 'ultimi 2 anni' },
  { key: '5a', months: 60, label: 'ultimi 5 anni' },
  { key: 'tutto', months: null, label: 'tutto il periodo' },
];

/** Primo giorno degli ultimi `months` mesi che terminano con l'ultimo giorno dei dati. */
function monthsBack(months) {
  const next = new Date(CFG.period[1] + 'T00:00:00Z');
  next.setUTCDate(next.getUTCDate() + 1);
  const start = new Date(Date.UTC(next.getUTCFullYear(), next.getUTCMonth() - months, 1));
  const monthDays = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0)).getUTCDate();
  start.setUTCDate(Math.min(next.getUTCDate(), monthDays));
  return start.toISOString().slice(0, 10);
}

/** Periodi selezionabili: quelli più brevi dei dati disponibili, più l'intero periodo. */
function periodOptions() {
  return PERIODS.filter((p) => p.months == null || monthsBack(p.months) > CFG.period[0]);
}

/** Primo giorno del periodo selezionato (null = intero periodo). */
function periodFrom() {
  const period = PERIODS.find((p) => p.key === state.period);
  return period && period.months ? monthsBack(period.months) : null;
}

const periodIndexCache = new WeakMap();

/** Primo indice dell'asse il cui intervallo (giorno, settimana, mese, trimestre) cade nel periodo. */
function periodIndex(dates) {
  const from = periodFrom();
  if (!from) return 0;
  const hit = periodIndexCache.get(dates);
  if (hit && hit.from === from) return hit.index;
  let index = Math.max(0, dates.length - 1);
  for (let i = 0; i < dates.length - 1; i++) {
    if (bucketStart(dates[i + 1]) > from) { index = i; break; }
  }
  periodIndexCache.set(dates, { from, index });
  return index;
}

/** Serie nazionale sullo stesso asse del livello (giorni, settimane, mesi o trimestri). */
function fullNationalSeries(level, combo) {
  const daily = (P.core.italy || {})[combo] || [];
  const axis = axisName(level);
  if (axis === 'daily') return daily;
  const resampled = ((P.core.italy_axes || {})[axis] || {})[combo];
  if (resampled) return resampled;
  return axisDates(level).length === daily.length ? daily : [];
}

function nationalSeries(level, combo) {
  return fullNationalSeries(level, combo).slice(periodIndex(axisDates(level)));
}

/** Serie dell'area limitata al periodo selezionato. */
function series(level, key, combo) {
  const full = fullSeries(level, key, combo);
  const start = periodIndex(full.dates);
  return { dates: full.dates.slice(start), values: full.values.slice(start) };
}

/** Serie dell'area sull'intero periodo dei dati (grafico dell'andamento). */
function fullSeries(level, key, combo) {
  const dates = axisDates(level);
  if (level === 'it') {
    return { dates, values: (P.core.italy || {})[combo] || [] };
  }
  if (level === 'region') {
    return { dates, values: ((P.core.regions || {})[key] || {})[combo] || [] };
  }
  if (level === 'province') {
    return { dates, values: ((P.provinces.provinces || {})[key] || {})[combo] || [] };
  }
  if (level === 'comune') {
    const i = P.comuni.index[key];
    return { dates, values: ((P.comuni.monthly || {})[combo] || [])[i] || [] };
  }
  return { dates: [], values: [] };
}

function statsOf(values) {
  const out = { n: 0, mean: null, min: null, max: null, minIndex: -1, maxIndex: -1, first: null, last: null, firstIndex: -1, lastIndex: -1, delta: null, volatility: null, p10: null, p50: null, p90: null };
  let sum = 0, prev = null, prevIndex = -2;
  const changes = [], clean = [];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v == null || !isFinite(v)) continue;
    clean.push(v);
    sum += v;
    out.n += 1;
    if (out.firstIndex < 0) { out.first = v; out.firstIndex = i; }
    out.last = v; out.lastIndex = i;
    if (out.min == null || v < out.min) { out.min = v; out.minIndex = i; }
    if (out.max == null || v > out.max) { out.max = v; out.maxIndex = i; }
    // solo periodi consecutivi: un salto sopra un intervallo senza dati non è una variazione
    if (prev != null && prev > 0 && i === prevIndex + 1) changes.push(v / prev - 1);
    prev = v;
    prevIndex = i;
  }
  if (out.n) {
    out.mean = sum / out.n;
    out.delta = out.first ? (out.last / out.first - 1) * 100 : null;
    if (changes.length > 2) {
      const avg = changes.reduce((a, b) => a + b, 0) / changes.length;
      const variance = changes.reduce((a, b) => a + (b - avg) ** 2, 0) / changes.length;
      out.volatility = Math.sqrt(variance) * 100;
    }
    const ordered = clean.slice().sort((a, b) => a - b);
    const pick = (f) => ordered[Math.round(f * (ordered.length - 1))];
    out.p10 = pick(0.1); out.p50 = pick(0.5); out.p90 = pick(0.9);
  }
  return out;
}

function lastKnown(level, key, combo) {
  if (level === 'province') return ((P.provinces.provinces_last || {})[key] || {})[combo] || null;
  if (level === 'comune') {
    const i = P.comuni.index[key];
    return ((P.comuni.last || {})[combo] || [])[i] || null;
  }
  return null;
}

function stationCount(level, key, combo) {
  if (level === 'it') {
    const counts = (P.core.italy_counts || {})[combo];
    return counts ? counts[counts.length - 1] : null;
  }
  if (level === 'region') {
    const counts = ((P.core.region_counts || {})[key] || {})[combo];
    return counts ? counts[counts.length - 1] : null;
  }
  const row = lastKnown(level, key, combo);
  if (row) return row[1];
  if (level === 'comune') {
    const meta = P.comuni.meta[key];
    return meta ? meta.n : null;
  }
  return null;
}

function labelFor(level, key) {
  if (level === 'it') return 'Italia';
  if (level === 'region') return (CFG.regionNames || {})[key] || key;
  if (level === 'province') return `${(CFG.provinceNames || {})[key] || key} (${key})`;
  if (level === 'comune') {
    const meta = P.comuni.meta[key];
    return meta ? `${meta.nome} (${meta.prov})` : key;
  }
  return key;
}

function childrenOf(level, key) {
  if (level === 'it') {
    return Object.keys(P.core.regions || {}).map((slug) => ({ level: 'region', key: slug }));
  }
  if (level === 'region') {
    const map2 = CFG.provinceToRegion || {};
    return Object.keys(P.provinces.provinces || {})
      .filter((code) => map2[code] === key)
      .map((code) => ({ level: 'province', key: code }));
  }
  if (level === 'province') {
    const prefix = key + '|';
    return P.comuni.keys.filter((k) => k.startsWith(prefix)).map((k) => ({ level: 'comune', key: k }));
  }
  return [];
}

function valueForMap(level, key, combo) {
  const data = series(level, key, combo);
  const stat = statsOf(data.values);
  if (state.metric === 'last') {
    const row = lastKnown(level, key, combo);
    return row ? row[0] : stat.last;
  }
  if (state.metric === 'delta') return stat.delta;
  if (state.metric === 'vs_country') {
    const nat = statsOf(nationalSeries(level, combo));
    return stat.mean != null && nat.mean != null ? (stat.mean - nat.mean) * 100 : null;
  }
  return stat.mean;
}

// Scale di colore: "viridis" per i valori assoluti, blu-rosso per gli scostamenti.
const PALETTE_SEQ = ['#414487', '#2a788e', '#22a884', '#7ad151', '#fde725'];
const PALETTE_DIV = ['#2166ac', '#67a9cf', '#f7f7f7', '#ef8a62', '#b2182b'];

function lerpPalette(stops, t) {
  const colors = stops.map((hex) => [
    parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16),
  ]);
  const clamped = Math.max(0, Math.min(1, t));
  const pos = clamped * (colors.length - 1);
  const i = Math.min(colors.length - 2, Math.floor(pos));
  const f = pos - i;
  const mix = colors[i].map((c, k) => Math.round(c + (colors[i + 1][k] - c) * f));
  return `rgb(${mix[0]},${mix[1]},${mix[2]})`;
}

function rampColor(t) {
  return lerpPalette(PALETTE_SEQ, t);
}

function divergingColor(cents) {
  const t = Math.max(-1, Math.min(1, cents / 12));  // ±12 c€/l saturano la scala
  return lerpPalette(PALETTE_DIV, 0.5 + t * 0.5);
}

// --------------------------------------------------------------------------------------
// Mappa
// --------------------------------------------------------------------------------------

function initMap() {
  map = L.map('map', { zoomControl: true, scrollWheelZoom: true, preferCanvas: true });
  // la mappa si allunga fino all'altezza del pannello accanto: Leaflet va avvisato
  if (window.ResizeObserver) new ResizeObserver(() => map.invalidateSize()).observe(map.getContainer());
  if (!CFG.boundaries) {
    map.setView([42.5, 12.5], 6);
    document.getElementById('map-hint').textContent = 'confini non disponibili: uso i centroidi degli impianti';
  }
  let manualChoice = false;
  const select = document.getElementById('tiles-select');
  if (select) {
    select.innerHTML = (CFG.tileProviders || []).map((p, i) => `<option value="${i}">${p.name}</option>`).join('');
    select.addEventListener('change', () => {
      manualChoice = true;
      useTileProvider(Number(select.value), true);
    });
  }
  useTileProvider(0, false);
  chooseTileProvider().then(({ index, verdict }) => {
    if (manualChoice || index <= 0) return;  // la scelta dell'utente prevale sulla verifica
    const providers = CFG.tileProviders || [];
    const reason = verdict !== 'blocked'
      ? 'non ha risposto alla verifica'
      : location.protocol === 'file:'
        ? 'non serve i tile alle pagine aperte da file locale'
        : 'ha rifiutato le richieste di tile';
    setTileNote(
      `il server «${providers[0].name}» ${reason}: uso «${providers[index].name}» ` +
      '(puoi cambiare sfondo dal menu qui sotto).',
      'warn',
    );
    useTileProvider(index, false);
  }).catch(() => { /* in caso di errore resta il provider predefinito */ });
}

/** Attiva un provider di tile; se i tile non sono accessibili passa al successivo. */
function useTileProvider(index, manual) {
  const providers = CFG.tileProviders || [];
  const provider = providers[index];
  if (!provider) return;
  if (tileLayer) {
    tileLayer.off('tileerror tileload');
    map.removeLayer(tileLayer);
  }
  const layer = L.tileLayer(provider.url, { maxZoom: 19, attribution: provider.attribution });
  tileLayer = layer;
  // solo errori consecutivi: un disservizio momentaneo non deve cambiare sfondo per sempre
  let errors = 0;
  layer.on('tileload', () => { errors = 0; });
  layer.on('tileerror', () => {
    errors += 1;
    if (errors < 3) return;
    layer.off('tileerror');
    const next = providers[index + 1];
    if (next) {
      setTileNote(`${provider.name} non raggiungibile: uso «${next.name}».`, 'warn');
      useTileProvider(index + 1, false);
    } else {
      setTileNote('nessun provider di mappe raggiungibile: la mappa resta senza sfondo.', 'warn');
    }
  });
  layer.addTo(map);
  const select = document.getElementById('tiles-select');
  if (select) select.value = String(index);
  if (manual) setTileNote(`mappa di base: ${provider.name}.`, 'info');
}

/** Tile di controllo per la verifica dei provider (zoom 6, Italia centrale). */
const PROBE_TILE = { z: '6', x: '33', y: '23' };

/**
 * Carica un tile come immagine con CORS attivo (stesso tipo di richiesta che usa poi
 * Leaflet per la mappa, così il verdetto riflette la realtà).
 */
function loadTileImage(url, timeoutMs = 6000) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.crossOrigin = 'anonymous';
    const timer = setTimeout(() => { image.src = ''; reject(new Error('timeout')); }, timeoutMs);
    image.onload = () => { clearTimeout(timer); resolve(image); };
    image.onerror = () => { clearTimeout(timer); reject(new Error('load')); };
    image.src = url;
  });
}

/**
 * Verifica se un provider serve tile utilizzabili.
 *
 * Il server principale di OpenStreetMap risponde alle pagine aperte da ``file://`` con
 * HTTP 200 e un'immagine "Access blocked" (strisce giallo/nero su fondo chiaro): lo stato
 * HTTP non basta, quindi si analizzano i pixel del tile di prova.
 * Valori restituiti: ``ok``, ``blocked`` oppure ``unknown`` (probe non conclusivo).
 */
async function probeTileProvider(provider) {
  const url = (provider.url || '')
    .replace('{z}', PROBE_TILE.z)
    .replace('{x}', PROBE_TILE.x)
    .replace('{y}', PROBE_TILE.y);
  let image;
  try {
    image = await loadTileImage(url);
  } catch (err) {
    return 'unknown';
  }
  const canvas = document.createElement('canvas');
  canvas.width = image.naturalWidth || 256;
  canvas.height = image.naturalHeight || 256;
  let data;
  try {
    const context = canvas.getContext('2d', { willReadFrequently: true });
    context.drawImage(image, 0, 0);
    data = context.getImageData(0, 0, canvas.width, canvas.height).data;
  } catch (err) {
    return 'unknown';  // canvas contaminato: non è possibile analizzare il tile
  }
  let white = 0;
  let dark = 0;
  let total = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 16) continue;
    total += 1;
    const r = data[i], g = data[i + 1], b = data[i + 2];
    if (r > 245 && g > 245 && b > 245) white += 1;
    else if ((r > 200 && g > 170 && b < 110) || (r < 45 && g < 45 && b < 45)) dark += 1;
  }
  if (!total) return 'blocked';
  if (white / total > 0.6 && dark / total > 0.01) return 'blocked';
  return 'ok';
}

/**
 * Sceglie il primo provider che serve tile utilizzabili (probe in parallelo).
 * Restituisce l'indice scelto e il verdetto sul provider predefinito.
 */
async function chooseTileProvider() {
  const providers = CFG.tileProviders || [];
  if (!providers.length) return { index: 0, verdict: 'unknown' };
  const verdicts = await Promise.all(providers.map((provider) => probeTileProvider(provider)));
  let index = verdicts.indexOf('ok');
  if (index < 0) index = verdicts.indexOf('unknown');
  if (index < 0) index = providers.length - 1;
  return { index, verdict: verdicts[0] };
}

function setTileNote(message, level) {
  const node = document.getElementById('tiles-note');
  if (!node) return;
  node.textContent = message || '';
  node.className = message ? `hint tiles-note ${level}` : 'hint tiles-note';
}

function clearOverlay() {
  if (overlay) { map.removeLayer(overlay); overlay = null; }
}

function drawMap() {
  clearOverlay();
  overlay = L.layerGroup().addTo(map);
  if (state.level === 'it' || state.level === 'region') {
    const drawn = P.boundaries ? drawBoundaries() : 0;
    if (!drawn) fallbackMarkers(state.level === 'it' ? 'region' : 'province');
  } else if (state.level === 'province') {
    const drawn = P.boundaries ? drawBoundaries() : 0;
    drawComuni();
    if (!drawn && !overlay.getLayers().length) fallbackMarkers('comune');
  } else if (state.level === 'comune') {
    drawStations();
  }
  const hint = document.getElementById('map-hint');
  if (hint) {
    hint.textContent = state.level === 'it'
      ? 'clicca una regione per entrare nel dettaglio'
      : state.level === 'region' ? 'clicca una provincia'
        : state.level === 'province' ? 'clicca un comune' : 'impianti dell\'ultimo giorno disponibile';
  }
  updateLegend();
}

function drawBoundaries() {
  const combo = scopeCombo();
  const features = [];
  const wanted = state.level === 'it' ? 4 : 6;
  for (const feature of ((P.boundaries || {}).features || [])) {
    const props = feature.properties;
    if (props.level !== wanted) continue;
    if (wanted === 6 && props.region !== state.region) continue;
    features.push(feature);
  }
  if (!features.length) return 0;
  const values = [];
  const valueByCode = {};
  for (const feature of features) {
    const code = state.level === 'it' ? feature.properties.slug : feature.properties.code;
    const level = state.level === 'it' ? 'region' : 'province';
    const value = valueForMap(level, code, combo);
    valueByCode[feature.properties.code] = value;
    if (value != null) values.push(value);
  }
  const lo = Math.min(...values), hi = Math.max(...values);
  const layer = L.geoJSON({ type: 'FeatureCollection', features }, {
    style: (feature) => {
      const value = valueByCode[feature.properties.code];
      const t = hi > lo ? (value - lo) / (hi - lo) : 0.5;
      const fill = state.metric === 'vs_country' ? divergingColor(value || 0) : rampColor(t);
      return { color: '#ffffff', weight: 1, fillColor: fill, fillOpacity: value == null ? 0.15 : 0.78 };
    },
    onEachFeature: (feature, layer2) => {
      const props = feature.properties;
      const code = state.level === 'it' ? props.slug : props.code;
      const level = state.level === 'it' ? 'region' : 'province';
      const label = state.level === 'it' ? (CFG.regionNames || {})[props.slug] || props.name : labelFor('province', props.code);
      const value = valueByCode[props.code];
      layer2.bindTooltip(`${label}<br><b>${state.metric === 'delta' ? fmtDelta(value) : state.metric === 'vs_country' ? fmtSigned(value) + ' c' + unit(state.fuel).slice(2) : fmtPrice(value) + ' ' + unit(state.fuel)}</b>`, { sticky: true });
      layer2.on('mouseover', () => layer2.setStyle({ weight: 2.5, color: '#06305f' }));
      layer2.on('mouseout', () => layer2.setStyle({ weight: 1, color: '#ffffff' }));
      layer2.on('click', () => drillTo(level, code));
    },
  });
  overlay.addLayer(layer);
  try {
    const bounds = layer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [12, 12], animate: false });
  } catch (err) { /* ignora */ }
  overlay.__bounds = layer;
  return features.length;
}

/** Fallback usato quando i confini OSM non sono disponibili: marker sui centroidi. */
function fallbackMarkers(childLevel) {
  const groups = new Map();
  P.comuni.keys.forEach((key) => {
    const meta = P.comuni.meta[key];
    if (!meta || meta.lat == null || meta.lon == null) return;
    let groupKey = null;
    if (childLevel === 'region') groupKey = CFG.provinceToRegion[meta.prov];
    else if (childLevel === 'province') groupKey = meta.prov;
    else if (childLevel === 'comune') groupKey = key;
    if (!groupKey) return;
    if (state.province && childLevel === 'comune' && meta.prov !== state.province) return;
    const acc = groups.get(groupKey) || { lat: 0, lon: 0, weight: 0, n: 0 };
    const weight = meta.n || 1;
    acc.lat += meta.lat * weight;
    acc.lon += meta.lon * weight;
    acc.weight += weight;
    acc.n += 1;
    groups.set(groupKey, acc);
  });
  if (!groups.size) return;
  const values = [...groups.keys()].map((key) => valueForMap(childLevel, key, scopeCombo())).filter((v) => v != null);
  const lo = Math.min(...values), hi = Math.max(...values);
  for (const [key, acc] of groups) {
    const value = valueForMap(childLevel, key, scopeCombo());
    const t = hi > lo ? (value - lo) / (hi - lo) : 0.5;
    const marker = L.circleMarker([acc.lat / acc.weight, acc.lon / acc.weight], {
      radius: 5 + Math.min(14, Math.sqrt(acc.n)), color: '#ffffff', weight: 1.5,
      fillColor: rampColor(t), fillOpacity: 0.85,
    });
    marker.bindTooltip(`${labelFor(childLevel, key)}<br><b>${fmtPrice(value)} ${unit(state.fuel)}</b>`, { sticky: true });
    marker.on('click', () => drillTo(childLevel, key));
    overlay.addLayer(marker);
  }
  try {
    const bounds = L.latLngBounds([...groups.values()].map((acc) => [acc.lat / acc.weight, acc.lon / acc.weight]));
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20], animate: false });
  } catch (err) { /* ignora */ }
}

function drawComuni() {
  const regionOnly = state.level === 'region';
  const points = [];
  for (const key of P.comuni.keys) {
    const meta = P.comuni.meta[key];
    if (!meta || meta.lat == null || meta.lon == null) continue;
    if (regionOnly) {
      if ((CFG.provinceToRegion[meta.prov] || null) !== state.region) continue;
    } else if (!key.startsWith(state.province + '|')) {
      continue;
    }
    points.push([key, meta]);
  }
  const all = points.map(([key]) => valueForMap('comune', key, scopeCombo())).filter((v) => v != null);
  if (!all.length) return;
  const lo = Math.min(...all), hi = Math.max(...all);
  points.forEach(([key, meta], i) => {
    const value = valueForMap('comune', key, scopeCombo());
    const t = hi > lo ? (value - lo) / (hi - lo) : 0.5;
    const fill = state.metric === 'vs_country' ? divergingColor(value || 0) : rampColor(t);
    const radius = 4 + Math.min(10, Math.sqrt(meta.n || 1));
    const marker = L.circleMarker([meta.lat, meta.lon], {
      radius, color: '#ffffff', weight: 1, fillColor: fill, fillOpacity: 0.85,
    });
    marker.bindTooltip(`${meta.nome}<br><b>${state.metric === 'delta' ? fmtDelta(value) : fmtPrice(value) + ' ' + unit(state.fuel)}</b><br>${meta.n} impianti`, { sticky: true });
    marker.on('click', () => drillTo('comune', key));
    overlay.addLayer(marker);
    void i;
  });
}

function drawStations() {
  const all = (P.stations && P.stations.comuni || {})[state.comune] || [];
  const combo = scopeCombo();
  const rows = all.filter((row) => row[6][combo] != null);
  const prices = rows.map((row) => row[6][combo]);
  const lo = Math.min(...prices), hi = Math.max(...prices);
  rows.forEach((row) => {
    const t = hi > lo ? (row[6][combo] - lo) / (hi - lo) : 0.5;
    const marker = L.circleMarker([row[4], row[5]], {
      radius: 6, color: '#ffffff', weight: 1.5, fillColor: rampColor(t), fillOpacity: 0.95,
    });
    marker.bindPopup(
      `<b>${row[1]}</b><br>${row[2]}<br><small>${row[3] || ''}</small><br>` +
      `<b style="font-size:1.05em">${fmtPrice(row[6][combo])} ${unit(state.fuel)}</b><br>` +
      `<small>${modeLabel(state.mode === 2 ? 0 : state.mode)}</small>`
    );
    overlay.addLayer(marker);
  });
  const meta = P.comuni.meta[state.comune];
  if (meta && meta.lat) map.setView([meta.lat, meta.lon], 12, { animate: false });
  if (!rows.length) {
    document.getElementById('map-hint').textContent = 'nessun impianto con questo carburante nel comune selezionato';
  }
}

function updateLegend() {
  const node = document.getElementById('legend');
  if (!node) return;
  const unitLabel = state.metric === 'mean' || state.metric === 'last'
    ? `prezzo ${state.metric === 'mean' ? 'medio del periodo' : 'ultimo disponibile'} (${unit(state.fuel)})`
    : state.metric === 'delta' ? 'variazione nel periodo (%)' : 'scostamento dalla media nazionale (c€)';
  const gradient = state.metric === 'vs_country'
    ? `linear-gradient(90deg,${PALETTE_DIV.join(',')})`
    : `linear-gradient(90deg,${PALETTE_SEQ.join(',')})`;
  node.innerHTML = `<span class="tag self">${state.fuel}</span> <span class="mode-badge">${modeLabel(state.mode === 2 ? 0 : state.mode)}</span>
    <span>basso</span><span class="bar" style="background:${gradient}"></span><span>alto</span>
    <span>&nbsp;·&nbsp;${unitLabel}</span>`;
}

// --------------------------------------------------------------------------------------
// Navigazione
// --------------------------------------------------------------------------------------

function drillTo(level, key) {
  if (level === 'region') { state.region = key; state.province = null; state.comune = null; state.level = 'region'; }
  else if (level === 'province') { state.province = key; state.comune = null; state.level = 'province'; }
  else if (level === 'comune') { state.comune = key; state.level = 'comune'; }
  navigate();
}

function goto(level) {
  if (level === 'it') { state.level = 'it'; state.region = null; state.province = null; state.comune = null; }
  else if (level === 'region') { state.level = 'region'; state.province = null; state.comune = null; }
  else if (level === 'province') { state.level = 'province'; state.comune = null; }
  navigate();
}

function currentKey() {
  if (state.level === 'it') return 'it';
  if (state.level === 'region') return state.region;
  if (state.level === 'province') return state.province;
  return state.comune;
}

/** Blocchi di dati necessari per mostrare un livello. */
function blocksFor(level) {
  const needed = [];
  if (level === 'region' || level === 'province') needed.push('provinces');
  if (level === 'province' || level === 'comune') needed.push('comuni');
  if (level === 'comune') needed.push('stations');
  return needed;
}

function showLoading(names) {
  const hint = document.getElementById('map-hint');
  if (hint && names.some((name) => !P[name])) hint.textContent = 'caricamento dei dati in corso…';
}

async function navigate(render = renderAll) {
  const needed = blocksFor(state.level);
  showLoading(needed);
  await Promise.all(needed.map(ensure));
  // nel frattempo la vista può essere cambiata: la disegna la navigazione che ne ha i dati
  if (blocksFor(state.level).some((name) => !P[name])) return;
  syncControls();
  render();
  writeHash();
}

// --------------------------------------------------------------------------------------
// Link condivisibile: la vista corrente è sempre nell'hash dell'URL
// --------------------------------------------------------------------------------------

const MODE_NAMES = ['servito', 'self', 'entrambe'];
const METRIC_NAMES = { mean: 'media', last: 'ultimo', delta: 'variazione', vs_country: 'scostamento' };
const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object || {}, key);

/** Aggiorna l'hash senza aggiungere voci alla cronologia; omette i valori predefiniti. */
function writeHash() {
  const params = new URLSearchParams();
  if (state.region) params.set('regione', state.region);
  if (state.province) params.set('provincia', state.province);
  if (state.comune) params.set('comune', state.comune.slice(state.comune.indexOf('|') + 1));
  if (state.fuel !== defaults.fuel) params.set('carburante', state.fuel);
  if (state.mode !== 0) params.set('modalita', MODE_NAMES[state.mode]);
  if (state.metric !== 'mean') params.set('metrica', METRIC_NAMES[state.metric]);
  if (state.period !== defaults.period) params.set('periodo', state.period);
  const hash = params.toString();
  try {
    history.replaceState(null, '', hash ? '#' + hash : location.pathname + location.search);
  } catch (err) { /* alcuni browser non lo consentono da file:// */ }
}

/**
 * Legge la vista dall'hash. Ogni valore viene confrontato con quelli presenti nei dati:
 * i valori sconosciuti si ignorano (le chiavi finiscono nell'HTML di breadcrumb e tabelle).
 */
async function applyHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const fuel = params.get('carburante');
  state.fuel = (CFG.fuels || []).some((f) => f.name === fuel) ? fuel : defaults.fuel;
  state.mode = Math.max(0, MODE_NAMES.indexOf(params.get('modalita')));
  state.metric = Object.keys(METRIC_NAMES).find((key) => METRIC_NAMES[key] === params.get('metrica')) || 'mean';
  const period = params.get('periodo');
  state.period = periodOptions().some((p) => p.key === period) ? period : defaults.period;

  state.level = 'it';
  state.region = state.province = state.comune = null;
  const regionNames = CFG.regionNames || {};
  const region = params.get('regione');
  const province = params.get('provincia');
  const provinceRegion = hasOwn(CFG.provinceToRegion, province) ? CFG.provinceToRegion[province] : null;
  if (hasOwn(CFG.provinceNames, province) && hasOwn(regionNames, provinceRegion)) {
    state.level = 'province';
    state.region = provinceRegion;
    state.province = province;
    const comune = params.get('comune');
    if (comune) {
      showLoading(['comuni']);
      await ensure('comuni');
      const key = `${province}|${comune.toUpperCase()}`;
      if (P.comuni && key in P.comuni.index) {
        state.level = 'comune';
        state.comune = key;
      }
    }
  } else if (hasOwn(regionNames, region)) {
    state.level = 'region';
    state.region = region;
  }
}

/** Allinea menu, descrizione del carburante e periodo mostrato allo stato corrente. */
function syncControls() {
  document.getElementById('fuel-select').value = state.fuel;
  document.getElementById('mode-select').value = String(state.mode);
  document.getElementById('metric-select').value = state.metric;
  document.getElementById('period-select').value = state.period;
  document.getElementById('fuel-desc').textContent = ((CFG.fuels || []).find((f) => f.name === state.fuel) || {}).description || '';
  const from = periodFrom();
  document.getElementById('period-label').textContent = `${from || CFG.period[0]} → ${CFG.period[1]}`;
  document.getElementById('period-extra').textContent = from ? ` (dati disponibili dal ${CFG.period[0]})` : '';
}

// --------------------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------------------

function renderAll() {
  for (const step of [renderCrumbs, renderKpis, drawMap, renderCharts, renderTables]) {
    try {
      step();
    } catch (err) {
      console.error('errore in ' + step.name, err);
    }
  }
}

function renderCrumbs() {
  const node = document.getElementById('crumbs');
  const parts = [];
  parts.push(`<button class="crumb ${state.level === 'it' ? 'current' : ''}" data-level="it">Italia</button>`);
  if (state.region) {
    parts.push('<span class="sep">›</span>');
    parts.push(`<button class="crumb ${state.level === 'region' ? 'current' : ''}" data-level="region">${labelFor('region', state.region)}</button>`);
  }
  if (state.province) {
    parts.push('<span class="sep">›</span>');
    parts.push(`<button class="crumb ${state.level === 'province' ? 'current' : ''}" data-level="province">${labelFor('province', state.province)}</button>`);
  }
  if (state.comune) {
    parts.push('<span class="sep">›</span>');
    parts.push(`<button class="crumb current">${labelFor('comune', state.comune)}</button>`);
  }
  parts.push('<span class="spacer"></span>');
  parts.push('<button class="ghost" id="btn-csv">scarica CSV delle serie</button>');
  node.innerHTML = parts.join(' ');
  node.querySelectorAll('.crumb[data-level]').forEach((button) => {
    button.addEventListener('click', () => goto(button.dataset.level));
  });
  document.getElementById('btn-csv').addEventListener('click', exportCsv);
}

function renderKpis() {
  const combo = scopeCombo();
  const data = series(state.level, currentKey(), combo);
  const stat = statsOf(data.values);
  const national = statsOf(nationalSeries(state.level, combo));
  const unitLabel = unit(state.fuel);
  const lastRow = lastKnown(state.level, currentKey(), combo);
  const stations = stationCount(state.level, currentKey(), combo);
  const granularity = granularityOf(state.level);
  const cards = [];

  const modeNote = state.mode === 2 ? ' · servito' : '';
  cards.push({ label: 'Prezzo medio del periodo', value: fmtPrice(stat.mean), unit: unitLabel, extra: `${nf.int.format(stat.n)} rilevazioni ${granularity}${modeNote}` });
  cards.push({ label: 'Ultimo valore', value: fmtPrice(lastRow ? lastRow[0] : stat.last), unit: unitLabel, extra: lastRow ? 'ultimo giorno disponibile' : data.dates[stat.lastIndex] || '' });
  cards.push({ label: 'Variazione nel periodo', value: fmtDelta(stat.delta), extra: `${data.dates[stat.firstIndex] || ''} → ${data.dates[stat.lastIndex] || ''}`, trend: stat.delta });
  cards.push({ label: 'Minimo', value: fmtPrice(stat.min), unit: unitLabel, extra: data.dates[stat.minIndex] || '' });
  cards.push({ label: 'Massimo', value: fmtPrice(stat.max), unit: unitLabel, extra: data.dates[stat.maxIndex] || '' });
  if (state.level !== 'it' && national.mean != null && stat.mean != null) {
    cards.push({ label: 'Scostamento da Italia', value: fmtSigned((stat.mean - national.mean) * 100), unit: 'c€/' + unitLabel.slice(2), extra: `media nazionale ${fmtPrice(national.mean)} ${unitLabel}`, trend: stat.mean - national.mean });
  }
  if (stat.volatility != null) {
    cards.push({ label: 'Volatilità', value: nf.dec1.format(stat.volatility) + '%', extra: `dev. std. variazioni ${granularity}` });
  }
  cards.push({ label: 'Impianti', value: stations ? nf.int.format(stations) : '–', extra: 'ultimo giorno disponibile' });

  document.getElementById('kpis').innerHTML = cards.map((card) => `
    <div class="kpi ${card.trend > 0.001 ? 'up' : card.trend < -0.001 ? 'down' : ''}">
      <div class="label">${card.label}</div>
      <div class="value">${card.value} <span class="unit">${card.unit || ''}</span></div>
      <div class="extra">${card.extra || ''}</div>
    </div>`).join('');
}

/** Individua i valori isolati (nessun vicino con dato): vanno evidenziati come punti,
 *  altrimenti un segmento di lunghezza 1 non sarebbe visibile in un grafico a linee. */
function isolatedMask(values) {
  const mask = new Array(values.length).fill(false);
  for (let i = 0; i < values.length; i++) {
    if (values[i] == null || !isFinite(values[i])) continue;
    const prev = i > 0 ? values[i - 1] : null;
    const next = i < values.length - 1 ? values[i + 1] : null;
    if (prev == null && next == null) mask[i] = true;
  }
  return mask;
}

const hasValue = (values, i) => i >= 0 && i < values.length && values[i] != null && isFinite(values[i]);

/** Punti da disegnare: i valori isolati e, se indicato, tutte le rilevazioni del feed. */
function pointOptions(values, color, feed) {
  const mask = isolatedMask(values);
  if (feed) {
    for (let i = feed.firstFeed; i < values.length; i++) if (hasValue(values, i)) mask[i] = true;
  }
  return {
    pointRadius: (ctx) => (mask[ctx.dataIndex] ? 3.8 : 0),
    pointBackgroundColor: color,
    pointBorderColor: '#ffffff',
    pointBorderWidth: 1.5,
    pointHitRadius: 8,
  };
}

/** Primo giorno coperto da un'etichetta dell'asse: giorno, settimana (lunedì), mese o trimestre. */
function bucketStart(label) {
  const text = String(label || '');
  if (/^\d{4}-\d{2}$/.test(text)) return `${text}-01`;
  const quarter = /^(\d{4})-T([1-4])$/.exec(text);
  if (quarter) return `${quarter[1]}-${String(Number(quarter[2]) * 3 - 2).padStart(2, '0')}-01`;
  return text;
}

/**
 * Parte dell'asse coperta solo dal feed giornaliero, ricavata dai metadati (fine degli
 * archivi trimestrali e giorno del feed) e non dall'andamento della serie:
 * - `firstFeed`: primo indice successivo all'ultimo giorno degli archivi;
 * - `liveIndex`: indice che contiene l'ultima rilevazione del feed;
 * - `gap`: indici non ancora pubblicati prima dell'ultima rilevazione (fascia grigia).
 */
function feedWindow(labels) {
  const { archiveEnd, liveDay } = CFG;
  if (!archiveEnd || !liveDay || liveDay <= archiveEnd) return null;
  let firstFeed = -1;
  let liveIndex = -1;
  labels.forEach((label, index) => {
    const start = bucketStart(label);
    if (firstFeed < 0 && start > archiveEnd) firstFeed = index;
    if (start <= liveDay) liveIndex = index;
  });
  if (firstFeed < 0 || liveIndex < firstFeed) return null;
  return { firstFeed, liveIndex, gap: liveIndex > firstFeed ? { start: firstFeed, end: liveIndex - 1 } : null };
}

function shortDate(iso) {
  const parts = String(iso || '').split('-');
  return parts.length === 3 ? `${parts[2]}/${parts[1]}` : String(iso || '');
}

/** Titolo del tooltip: segnala le rilevazioni del feed (con il giorno, se l'asse non è giornaliero). */
function feedTooltipTitle(items, feed, daily) {
  const item = items[0];
  if (!item) return '';
  if (!feed || item.dataIndex < feed.firstFeed) return item.label;
  if (!daily && item.dataIndex === feed.liveIndex) return `${item.label} · feed giornaliero del ${CFG.liveDay}`;
  return `${item.label} · feed giornaliero`;
}

/** Annotazioni del grafico di andamento: fascia non pubblicata, rilevazioni del feed, p10–p90. */
function buildFeedAnnotation(labels, datasets, feed, daily) {
  const annotation = { gap: null, points: [], whiskers: [] };
  if (!feed) return annotation;
  annotation.gap = feed.gap;
  const pointDate = (index) => {
    if (daily) return shortDate(labels[index]);
    return index === feed.liveIndex ? shortDate(CFG.liveDay) : labels[index];
  };
  datasets.forEach((dataset, datasetIndex) => {
    if (!dataset.labelFeed) return;
    const values = dataset.data || [];
    for (let index = feed.firstFeed; index < values.length; index++) {
      // un'etichetta per ogni tratto del feed, sul suo ultimo punto
      if (!hasValue(values, index) || hasValue(values, index + 1)) continue;
      annotation.points.push({
        datasetIndex,
        index,
        live: index === feed.liveIndex,
        label: `${pointDate(index)} · ${fmtPrice(values[index])} ${unit(state.fuel)}`,
        note: 'feed giornaliero',
      });
    }
  });
  const low = datasets.findIndex((dataset) => dataset.bandRole === 'p10');
  const high = datasets.findIndex((dataset) => dataset.bandRole === 'p90');
  if (low >= 0 && high >= 0) {
    // su un giorno isolato la banda riempita non si vede: si disegna un segmento verticale
    const lows = datasets[low].data;
    const highs = datasets[high].data;
    isolatedMask(lows).forEach((flag, index) => {
      if (flag && hasValue(highs, index)) {
        annotation.whiskers.push({ index, low: lows[index], high: highs[index], datasets: [low, high] });
      }
    });
  }
  return annotation;
}

const ANNOTATION_FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';

/** Plugin Chart.js: fascia dei dati non ancora pubblicati ed etichette del feed giornaliero. */
const feedAnnotationPlugin = {
  id: 'feedAnnotation',
  afterDatasetsDraw(chart) {
    const config = (chart.options.plugins || {}).feedAnnotation || {};
    const { ctx, chartArea, scales } = chart;
    if (!chartArea || !scales.x || !scales.y) return;
    const occupied = [];   // etichette già disegnate
    const markers = [];    // punti e segmenti p10–p90
    const hits = (list, box) => list.some((other) =>
      box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top);
    const overlaps = (box) => hits(occupied, box) || hits(markers, box);
    const inside = (box) => box.top >= chartArea.top && box.bottom <= chartArea.bottom;
    const clampX = (x) => Math.max(chartArea.left, Math.min(chartArea.right, x));
    // con lo zoom gli elementi fuori dall'intervallo visibile hanno coordinate non aggiornate
    const visible = (index) => index >= scales.x.min && index <= scales.x.max;

    ctx.save();
    let band = null;
    if (config.gap) {
      const left = clampX(scales.x.getPixelForValue(config.gap.start - 0.5));
      const right = clampX(scales.x.getPixelForValue(config.gap.end + 0.5));
      if (right > left) {
        band = { left, right };
        ctx.fillStyle = 'rgba(110,120,135,.10)';
        ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = 'rgba(110,120,135,.5)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(left, chartArea.top);
        ctx.lineTo(left, chartArea.bottom);
        ctx.moveTo(right, chartArea.top);
        ctx.lineTo(right, chartArea.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    ctx.strokeStyle = 'rgba(0,87,184,.45)';
    ctx.lineWidth = 2;
    for (const whisker of config.whiskers || []) {
      if (!visible(whisker.index) || !whisker.datasets.every((index) => chart.isDatasetVisible(index))) continue;
      const x = scales.x.getPixelForValue(whisker.index);
      const top = scales.y.getPixelForValue(whisker.high);
      const bottom = scales.y.getPixelForValue(whisker.low);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.moveTo(x - 4, top);
      ctx.lineTo(x + 4, top);
      ctx.moveTo(x - 4, bottom);
      ctx.lineTo(x + 4, bottom);
      ctx.stroke();
      markers.push({ left: x - 5, right: x + 5, top: top - 2, bottom: bottom + 2 });
    }

    const placed = [];
    for (const point of config.points || []) {
      if (!visible(point.index) || !chart.isDatasetVisible(point.datasetIndex)) continue;
      const element = chart.getDatasetMeta(point.datasetIndex).data[point.index];
      if (!element || !isFinite(element.x) || !isFinite(element.y)) continue;
      markers.push({ left: element.x - 5, right: element.x + 5, top: element.y - 5, bottom: element.y + 5 });
      placed.push({ point, x: element.x, y: element.y });
    }
    // prima l'ultima rilevazione (etichetta sempre presente), poi le altre da destra a sinistra:
    // queste ricevono l'etichetta, anche compatta e senza nota, solo se c'è spazio libero
    placed.sort((a, b) => (b.point.live - a.point.live) || b.x - a.x || a.y - b.y);
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    placed.forEach(({ point, x, y }) => {
      ctx.font = `600 10px ${ANNOTATION_FONT}`;
      const labelWidth = ctx.measureText(point.label).width;
      ctx.font = `10px ${ANNOTATION_FONT}`;
      const noteWidth = point.note ? ctx.measureText(point.note).width : 0;
      const variants = point.note ? [true, false] : [false];
      const candidates = [];
      for (const withNote of variants) {
        const width = Math.max(labelWidth, withNote ? noteWidth : 0);
        const height = withNote ? 24 : 12;
        const right = x + 8;
        const left = x - 8 - width;
        const sides = right + width <= chartArea.right ? [right, left] : [left];
        for (const side of sides) {
          if (side < chartArea.left) continue;
          for (const top of [y - height / 2, y - 6 - height, y + 6]) {
            candidates.push({ left: side, right: side + width, top, bottom: top + height, withNote });
          }
        }
      }
      let box = candidates.find((c) => inside(c) && !overlaps(c));
      if (!box && point.live) box = candidates.find((c) => inside(c) && !hits(occupied, c)) || candidates.find(inside);
      if (!box) return;
      occupied.push(box);
      // alone chiaro: il testo resta leggibile anche sopra un punto vicino
      ctx.strokeStyle = 'rgba(255,255,255,.85)';
      ctx.lineWidth = 3;
      ctx.lineJoin = 'round';
      const text = (content, top, color, weight) => {
        ctx.font = `${weight} 10px ${ANNOTATION_FONT}`;
        ctx.strokeText(content, box.left, top);
        ctx.fillStyle = color;
        ctx.fillText(content, box.left, top);
      };
      text(point.label, box.top, '#16202c', 600);
      if (box.withNote) text(point.note, box.top + 13, '#5c6b7a', 400);
    });

    if (band) {
      ctx.font = `10px ${ANNOTATION_FONT}`;
      const lines = wrapText(ctx, 'dati non ancora pubblicati dal MIMIT per questo periodo', band.right - band.left - 12, 4);
      if (lines.length) {
        const width = Math.max(...lines.map((line) => ctx.measureText(line).width));
        const height = lines.length * 12;
        const center = (band.left + band.right) / 2;
        const make = (top) => ({ left: center - width / 2, right: center + width / 2, top, bottom: top + height });
        // di norma in basso: l'ultima rilevazione è spesso il valore più alto del grafico
        const candidates = [make(chartArea.bottom - height - 6), make(chartArea.top + 6)];
        const box = candidates.find((c) => !overlaps(c)) || candidates[0];
        ctx.fillStyle = '#5c6b7a';
        ctx.textAlign = 'center';
        lines.forEach((line, i) => ctx.fillText(line, center, box.top + i * 12));
      }
    }
    ctx.restore();
  },
};

/** Divide un testo in righe larghe al massimo `maxWidth`; nessuna riga se non ci sta. */
function wrapText(ctx, text, maxWidth, maxLines) {
  const lines = [];
  for (const word of text.split(' ')) {
    const candidate = lines.length ? `${lines[lines.length - 1]} ${word}` : word;
    if (lines.length && ctx.measureText(candidate).width <= maxWidth) lines[lines.length - 1] = candidate;
    else if (ctx.measureText(word).width <= maxWidth) lines.push(word);
    else return [];
  }
  return lines.length <= maxLines ? lines : [];
}

/** Mostra «torna al periodo» solo quando il grafico dell'andamento è ingrandito o spostato. */
function updateZoomReset({ chart }) {
  document.getElementById('btn-zoom-reset').hidden = !chart.isZoomedOrPanned();
}

function resetTrendZoom() {
  if (charts.trend && typeof charts.trend.resetZoom === 'function') charts.trend.resetZoom();
  document.getElementById('btn-zoom-reset').hidden = true;
}

function toggleChart(chartId, emptyMessage) {
  const canvas = document.getElementById(chartId);
  if (!canvas) return;
  const wrap = canvas.parentElement;
  let note = wrap.querySelector('.chart-empty');
  if (emptyMessage) {
    canvas.style.display = 'none';
    if (!note) {
      note = document.createElement('div');
      note.className = 'empty chart-empty';
      wrap.appendChild(note);
    }
    note.textContent = emptyMessage;
  } else {
    canvas.style.display = '';
    if (note) note.remove();
  }
}

function renderCharts() {
  if (typeof Chart === 'undefined') return;
  for (const key of Object.keys(charts)) {
    try { charts[key].destroy(); } catch (err) { /* ignora */ }
    delete charts[key];
  }
  const combo = scopeCombo();
  const key = currentKey();
  // l'andamento mostra tutti i dati (con lo zoom si esce dal periodo), il resto solo il periodo
  const full = fullSeries(state.level, key, combo);
  const national = fullNationalSeries(state.level, combo);
  const unitLabel = unit(state.fuel);
  const labels = full.dates;
  const feed = feedWindow(labels);
  const daily = granularityOf(state.level) === 'giornaliere';
  const periodStart = periodIndex(labels);

  // --- andamento nel tempo ---
  const datasets = [];
  const baseColor = '#0057b8';
  if (state.mode === 2) {
    const other = fullSeries(state.level, key, state.fuel + '|1');
    datasets.push({
      label: `${state.fuel} · self service`, data: other.values, borderColor: '#1e7d4f',
      backgroundColor: 'rgba(30,125,79,.10)', borderWidth: 2, tension: .2, spanGaps: false,
      labelFeed: true, ...pointOptions(other.values, '#1e7d4f', feed),
    });
    datasets.push({
      label: `${state.fuel} · servito`, data: full.values, borderColor: baseColor,
      backgroundColor: 'rgba(0,87,184,.10)', borderWidth: 2, tension: .2, spanGaps: false,
      labelFeed: true, ...pointOptions(full.values, baseColor, feed),
    });
  } else {
    if (state.level !== 'it') {
      datasets.push({
        label: 'Italia', data: national, borderColor: '#95a5b6', backgroundColor: 'transparent',
        borderWidth: 1.5, borderDash: [5, 4], tension: .2, spanGaps: false,
        ...pointOptions(national, '#95a5b6'),
      });
    }
    if (state.level === 'it' && (P.core.dispersion || {})[combo]) {
      const band = P.core.dispersion[combo];
      datasets.push({
        label: 'province · 10° percentile', data: band.p10, borderColor: 'rgba(0,87,184,.25)',
        backgroundColor: 'rgba(0,87,184,.12)', borderWidth: 0, pointRadius: 0, fill: '+1', spanGaps: false,
        bandRole: 'p10',
      });
      datasets.push({
        label: 'province · 90° percentile', data: band.p90, borderColor: 'rgba(0,87,184,.25)',
        backgroundColor: 'transparent', borderWidth: 0, pointRadius: 0, spanGaps: false,
        bandRole: 'p90',
      });
    }
    datasets.push({
      label: `${labelFor(state.level, key)} · ${modeLabel(state.mode)}`, data: full.values,
      borderColor: baseColor, backgroundColor: 'rgba(0,87,184,.12)', borderWidth: 2.2,
      tension: .2, spanGaps: false, fill: state.level === 'it' && !(P.core.dispersion || {})[combo],
      labelFeed: true, ...pointOptions(full.values, baseColor, feed),
    });
  }
  const trendHint = document.getElementById('trend-hint');
  if (trendHint) {
    trendHint.textContent = feed
      ? (feed.gap ? 'fascia grigia: non ancora negli archivi MIMIT · ' : '') + 'punti a destra: feed giornaliero'
      : '';
  }
  charts.trend = new Chart(document.getElementById('chart-trend'), {
    type: 'line',
    data: { labels, datasets },
    plugins: [feedAnnotationPlugin],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        feedAnnotation: buildFeedAnnotation(labels, datasets, feed, daily),
        legend: { labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            title: (items) => feedTooltipTitle(items, feed, daily),
            label: (ctx) => `${ctx.dataset.label}: ${fmtPrice(ctx.parsed.y)} ${unitLabel}`,
          },
        },
        zoom: {
          // trascina = zoom sull'intervallo, Maiusc+trascina = scorri, Ctrl+rotella = zoom;
          // su touch: pizzica = zoom, trascina = scorri (hammerjs ignora il tasto modificatore)
          limits: { x: { min: 0, max: labels.length - 1, minRange: Math.min(7, labels.length - 1) } },
          pan: { enabled: true, mode: 'x', modifierKey: 'shift', onPanComplete: updateZoomReset },
          zoom: {
            mode: 'x',
            drag: { enabled: true, threshold: 5, backgroundColor: 'rgba(0,87,184,.10)', borderColor: 'rgba(0,87,184,.45)', borderWidth: 1 },
            wheel: { enabled: true, modifierKey: 'ctrl' },
            pinch: { enabled: true },
            onZoomComplete: updateZoomReset,
          },
        },
      },
      scales: {
        x: { min: periodStart || undefined, ticks: { maxTicksLimit: 10, font: { size: 10 } }, grid: { display: false } },
        y: { ticks: { callback: (v) => nf.price.format(v), font: { size: 10 } } },
      },
    },
  });
  // hammerjs blocca lo scorrimento della pagina sul grafico: quello verticale resta al browser
  charts.trend.canvas.style.touchAction = 'pan-y';
  document.getElementById('btn-zoom-reset').hidden = true;

  // --- stagionalità (medie mensili per anno, nel periodo selezionato) ---
  const data = series(state.level, key, combo);
  const byYear = new Map();
  for (let i = 0; i < data.values.length; i++) {
    const value = data.values[i];
    if (value == null || !isFinite(value)) continue;
    const iso = bucketStart(data.dates[i]);
    const year = iso.slice(0, 4);
    const month = Number(iso.slice(5, 7));
    if (!year || !month) continue;
    if (!byYear.has(year)) byYear.set(year, Array.from({ length: 12 }, () => ({ sum: 0, n: 0 })));
    const slot = byYear.get(year)[month - 1];
    slot.sum += value; slot.n += 1;
  }
  const years = [...byYear.keys()].sort();
  const monthLabels = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic'];
  const seasonDatasets = years.map((year, index) => {
    const means = byYear.get(year).map((slot) => (slot.n ? slot.sum / slot.n : null));
    // l'anno più recente è il più scuro, i precedenti sfumano verso il verde
    const color = lerpPalette(PALETTE_SEQ, years.length > 1 ? (1 - index / (years.length - 1)) * 0.8 : 0);
    return {
      label: year, data: means, borderWidth: 2, tension: .25, spanGaps: false,
      borderColor: color, backgroundColor: 'transparent', ...pointOptions(means, color),
    };
  });
  charts.season = new Chart(document.getElementById('chart-season'), {
    type: 'line',
    data: { labels: monthLabels, datasets: seasonDatasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmtPrice(ctx.parsed.y)} ${unitLabel}` } } },
      scales: { y: { ticks: { callback: (v) => nf.price.format(v), font: { size: 10 } } }, x: { grid: { display: false } } },
    },
  });

  // --- confronto tra aree (classifica) ---
  const children = state.level === 'comune' ? [] : childrenOf(state.level, key);
  const ranked = children.map((child) => {
    const stat = statsOf(series(child.level, child.key, combo).values);
    return { label: labelFor(child.level, child.key), value: stat.mean, delta: stat.delta, count: stat.n, level: child.level, key: child.key };
  }).filter((row) => row.value != null);
  ranked.sort((a, b) => b.value - a.value);
  const top = ranked.slice(0, 8);
  const bottom = ranked.slice(-8).reverse();
  const shown = ranked.length > 20 ? top.concat(bottom) : ranked.slice(0, 16);
  if (!shown.length) {
    delete charts.rank;
    toggleChart('chart-rank', 'Nessun confronto disponibile a questo livello: seleziona un\'area più ampia (o un\'altra metrica).');
  } else {
    toggleChart('chart-rank', null);
  }
  charts.rank = shown.length ? new Chart(document.getElementById('chart-rank'), {
    type: 'bar',
    data: {
      labels: shown.map((row) => row.label),
      datasets: [{ label: `prezzo medio (${unitLabel})`, data: shown.map((row) => row.value), backgroundColor: shown.map((row) => rampColor(ranked.length ? ranked.indexOf(row) / Math.max(1, ranked.length - 1) : 0.5)) }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => `${fmtPrice(ctx.parsed.x)} ${unitLabel}` } } },
      scales: { x: { ticks: { callback: (v) => nf.price.format(v), font: { size: 10 } } }, y: { ticks: { font: { size: 10 } } } },
      onClick: (event, elements) => {
        if (!elements.length) return;
        const row = shown[elements[0].index];
        if (row && row.level !== 'comune') drillTo(row.level, row.key);
        else if (row) drillTo('comune', row.key);
      },
    },
  }) : null;

  // --- distribuzione (istogramma a classi) ---
  const values = ranked.map((row) => row.value);
  if (!values.length) {
    toggleChart('chart-dist', 'Nessuna distribuzione da mostrare a questo livello.');
    return;
  }
  toggleChart('chart-dist', null);
  const lowest = Math.min(...values);
  const highest = Math.max(...values);
  const binCount = Math.max(4, Math.min(14, Math.round(Math.sqrt(values.length)) + 2));
  const binWidth = (highest - lowest) / binCount || 0.001;
  const counts = new Array(binCount).fill(0);
  values.forEach((value) => {
    const index = Math.min(binCount - 1, Math.floor((value - lowest) / binWidth));
    counts[index] += 1;
  });
  const distLabels = counts.map((_, i) => nf.price.format(lowest + binWidth * (i + 0.5)));
  const distColors = counts.map((_, i) => rampColor(binCount > 1 ? i / (binCount - 1) : 0.5));
  charts.dist = new Chart(document.getElementById('chart-dist'), {
    type: 'bar',
    data: {
      labels: distLabels,
      datasets: [{ label: 'numero di aree', data: counts, backgroundColor: distColors }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => `${items[0].label} ${unitLabel}`,
            label: (ctx) => `${ctx.parsed.y} aree`,
          },
        },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 8, font: { size: 9 }, maxRotation: 0 }, grid: { display: false } },
        y: { ticks: { precision: 0, font: { size: 10 } } },
      },
    },
  });
}

function renderTables() {
  const combo = scopeCombo();
  const unitLabel = unit(state.fuel);
  const children = state.level === 'comune' ? [] : childrenOf(state.level, key_of_level());
  const rows = children.map((child) => {
    const stat = statsOf(series(child.level, child.key, combo).values);
    const last = lastKnown(child.level, child.key, combo);
    return {
      level: child.level, key: child.key, label: labelFor(child.level, child.key),
      mean: stat.mean, delta: stat.delta, min: stat.min, max: stat.max,
      last: last ? last[0] : stat.last, stations: stationCount(child.level, child.key, combo) || last?.[1],
      volatility: stat.volatility,
    };
  }).filter((row) => row.mean != null);
  rows.sort((a, b) => b.mean - a.mean);

  const tableRows = rows.map((row) => `
    <tr class="clickable" data-level="${row.level}" data-key="${row.key}">
      <td>${row.label}</td>
      <td>${row.stations ? nf.int.format(row.stations) : '–'}</td>
      <td>${fmtPrice(row.mean)}</td>
      <td>${fmtPrice(row.last)}</td>
      <td class="${signedClass(row.delta)}">${fmtDelta(row.delta)}</td>
      <td>${fmtPrice(row.min)}</td>
      <td>${fmtPrice(row.max)}</td>
    </tr>`).join('');

  document.getElementById('table-areas').innerHTML = rows.length ? `
    <div class="panel-head"><h3>${state.level === 'it' ? 'Regioni' : state.level === 'region' ? 'Province' : 'Comuni'} — clicca una riga per il dettaglio</h3>
      <span class="hint">${rows.length} aree · prezzi in ${unitLabel}</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Area</th><th>Impianti</th><th>Media</th><th>Ultimo</th><th>Δ periodo</th><th>Min</th><th>Max</th></tr></thead>
      <tbody>${tableRows}</tbody></table></div>` : '<div class="empty">Nessuna area disponibile a questo livello.</div>';

  document.querySelectorAll('#table-areas tr.clickable').forEach((tr) => {
    tr.addEventListener('click', () => drillTo(tr.dataset.level, tr.dataset.key));
  });

  // tabella annuale
  const data = series(state.level, currentKey(), combo);
  const byYear = new Map();
  for (let i = 0; i < data.values.length; i++) {
    const value = data.values[i];
    if (value == null) continue;
    const year = (data.dates[i] || '').slice(0, 4);
    if (!year) continue;
    if (!byYear.has(year)) byYear.set(year, { sum: 0, n: 0, min: value, max: value, last: null });
    const slot = byYear.get(year);
    slot.sum += value; slot.n += 1;
    if (value < slot.min) slot.min = value;
    if (value > slot.max) slot.max = value;
    slot.last = value;
  }
  const yearRows = [...byYear.entries()].sort().map(([year, slot]) => {
    const mean = slot.sum / slot.n;
    return { year, mean, min: slot.min, max: slot.max, last: slot.last, n: slot.n };
  });
  yearRows.forEach((row, i) => {
    row.delta = i > 0 ? (row.mean / yearRows[i - 1].mean - 1) * 100 : null;
  });
  document.getElementById('table-years').innerHTML = yearRows.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>Anno</th><th>Media</th><th>Min</th><th>Max</th><th>Δ vs anno prec.</th><th>N. rilevazioni</th></tr></thead>
      <tbody>${yearRows.map((row) => `<tr>
        <td>${row.year}</td><td>${fmtPrice(row.mean)}</td><td>${fmtPrice(row.min)}</td><td>${fmtPrice(row.max)}</td>
        <td class="${signedClass(row.delta)}">${fmtDelta(row.delta)}</td><td>${nf.int.format(row.n)}</td></tr>`).join('')}
      </tbody></table></div>` : '<div class="empty">Nessun dato annuale.</div>';

  // tabella impianti
  const stationPanel = document.getElementById('panel-stations');
  if (state.level === 'comune' && P.stations) {
    stationPanel.hidden = false;
    const all = (P.stations.comuni || {})[state.comune] || [];
    const list = all
      .map((row) => ({ row, price: row[6][combo] }))
      .filter((item) => item.price != null)
      .sort((a, b) => a.price - b.price);
    document.getElementById('table-stations').innerHTML = list.length ? `
      <div class="panel-head"><h3>Impianti del comune — ${P.stations.day}</h3>
        <span class="hint">${list.length} impianti con ${state.fuel} ${modeLabel(state.mode === 2 ? 0 : state.mode)} · prezzi in ${unitLabel}</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Impianto</th><th>Insegna</th><th>Indirizzo</th><th id="th-price">Prezzo</th></tr></thead>
        <tbody>${list.map((item) => `<tr>
          <td>${item.row[1]}</td><td>${item.row[2] || '–'}</td><td>${item.row[3] || '–'}</td>
          <td>${fmtPrice(item.price)}</td></tr>`).join('')}
        </tbody></table></div>` : '<div class="empty">Nessun impianto con questo carburante.</div>';
  } else {
    stationPanel.hidden = true;
  }
}

function key_of_level() {
  return state.level === 'it' ? 'it' : state.level === 'region' ? state.region : state.level === 'province' ? state.province : state.comune;
}

// --------------------------------------------------------------------------------------
// Export CSV
// --------------------------------------------------------------------------------------

function exportCsv() {
  const combo = scopeCombo();
  const data = series(state.level, currentKey(), combo);
  const lines = ['data;' + `${state.fuel}_${modeLabel(state.mode === 2 ? 0 : state.mode)}`.replace(/;/g, '')];
  for (let i = 0; i < data.dates.length; i++) {
    const value = data.values[i];
    lines.push(`${data.dates[i]};${value == null ? '' : value.toFixed(3).replace('.', ',')}`);
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `prezzi_${state.fuel}_${labelFor(state.level, currentKey()).replace(/[^A-Za-z0-9]+/g, '_')}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
}

// --------------------------------------------------------------------------------------
// Avvio
// --------------------------------------------------------------------------------------

async function main() {
  try {
    P.core = await ensure('core');
  } catch (err) {
    document.getElementById('kpis').innerHTML = `<div class="banner">Impossibile leggere i dati del report: ${err.message}</div>`;
    return;
  }
  if (!P.core) {
    document.getElementById('kpis').innerHTML = '<div class="banner">Dati non disponibili nella pagina.</div>';
    return;
  }
  const fuels = (CFG.fuels || []).map((f) => f.name);
  defaults.fuel = fuels.includes('Gasolio') ? 'Gasolio' : fuels[0];
  defaults.period = periodOptions().some((p) => p.key === '1a') ? '1a' : 'tutto';
  document.getElementById('fuel-select').innerHTML = (CFG.fuels || []).map((f) => `<option value="${f.name}">${f.name}</option>`).join('');
  document.getElementById('period-select').innerHTML = periodOptions().map((p) => `<option value="${p.key}">${p.label}</option>`).join('');
  document.getElementById('fuel-select').addEventListener('change', (event) => {
    state.fuel = event.target.value;
    navigate();
  });
  document.getElementById('mode-select').addEventListener('change', (event) => {
    state.mode = Number(event.target.value);
    navigate();
  });
  document.getElementById('metric-select').addEventListener('change', (event) => {
    state.metric = event.target.value;
    navigate(drawMap);
  });
  document.getElementById('period-select').addEventListener('change', (event) => {
    state.period = event.target.value;
    navigate();
  });
  // un link incollato nella stessa scheda cambia solo l'hash: nessun ricaricamento
  window.addEventListener('hashchange', async () => {
    await applyHash();
    navigate();
  });

  const zoomHint = document.getElementById('zoom-hint');
  if (!Chart.registry.plugins.get('zoom')) zoomHint.hidden = true;
  else if (window.matchMedia('(pointer: coarse)').matches) {
    zoomHint.textContent = 'pizzica il grafico per lo zoom · trascina in orizzontale per scorrere';
  }
  document.getElementById('chart-trend').addEventListener('dblclick', resetTrendZoom);
  document.getElementById('btn-zoom-reset').addEventListener('click', resetTrendZoom);

  if (CFG.boundaries) await ensure('boundaries');
  initMap();
  await applyHash();
  await navigate();
}

// lo script segue i dati nazionali e i confini: l'app parte mentre il resto della pagina arriva
main();
