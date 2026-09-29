"""Configurazione del tool: fonti open data, classificazione carburanti, tabelle geografiche.

Tutte le fonti sono dataset pubblici del MIMIT (Ministero delle Imprese e del Made in
Italy) raggiungibili dalle pagine open data:

* archivio storico trimestrale:
  https://www.mimit.gov.it/it/open-data/elenco-dataset/carburanti-archivio-prezzi
* prezzi e anagrafica del giorno (feed giornaliero):
  https://www.mimit.gov.it/it/open-data/elenco-dataset/carburanti-prezzi-praticati-e-anagrafica-degli-impianti
"""

from __future__ import annotations

import unicodedata

# --------------------------------------------------------------------------------------
# Fonti
# --------------------------------------------------------------------------------------

#: Pagina "elenco dataset" da cui vengono scoperti gli archivi trimestrali.
SOURCE_PAGE = (
    "https://www.mimit.gov.it/it/open-data/elenco-dataset/carburanti-archivio-prezzi"
)

#: Host degli archivi storici (tar.gz con un file CSV per ogni giorno del trimestre).
ARCHIVE_BASE = "https://opendatacarburanti.mise.gov.it"

#: Snapshot "live" (aggiornati ogni giorno) usati per l'anagrafica degli impianti attivi.
LIVE_ANAGRAFICA = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
LIVE_PREZZI = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"

#: Categorie di archivio pubblicate dal MIMIT.
CATEGORIES = ("prezzo_alle_8", "anagrafica_impianti_attivi")

USER_AGENT = "mimit-fuel-report/1.0 (open data report generator; +" + SOURCE_PAGE + ")"

#: Provider di mappe di base, in ordine di preferenza: i tile di
#: ``tile.openstreetmap.org`` bloccano le richieste che arrivano da pagine aperte con
#: ``file://`` in alcuni browser (politica d'uso dei tile OSM), quindi il report verifica
#: all'avvio quale provider è utilizzabile. Tutti i provider usano dati OpenStreetMap.
TILE_PROVIDERS = (
    {
        "name": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
    {
        "name": "OpenStreetMap (FOSSGIS)",
        "url": "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors '
        "(tile FOSSGIS)",
    },
    {
        "name": "OSM Humanitarian",
        "url": "https://a.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors '
        "(stile HOT)",
    },
)
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/lookup"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

VENDOR_LIBS = {
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "chart.js": "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
    # zoom e pan del grafico dell'andamento; hammerjs serve per pinch e pan su touch
    "hammer.js": "https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js",
    "chartjs-plugin-zoom.js": "https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js",
}


# --------------------------------------------------------------------------------------
# Classificazione dei carburanti
# --------------------------------------------------------------------------------------
# Il dataset contiene ~60 etichette commerciali diverse ("Blue Diesel", "HVOlution",
# "Hi-Q Diesel", ...). Per rendere confrontabili le serie storiche le raggruppiamo in
# poche categorie merceologiche.

FUEL_ORDER = ("Benzina", "Gasolio", "GPL", "Metano", "GNL", "Speciali")

FUEL_DESCRIPTIONS = {
    "Benzina": "Benzina senza piombo (95 ottani), esclusi i prodotti premium",
    "Gasolio": "Gasolio per autotrazione, esclusi diesel premium/HVO",
    "GPL": "GPL per autotrazione (prezzo al litro)",
    "Metano": "Metano per autotrazione (prezzo al kg)",
    "GNL": "Gas naturale liquefatto e L-GNC (prezzo al kg)",
    "Speciali": "Carburanti premium / speciali (HVO, 98 ottani, artico, ...)",
}

#: Etichette "premium" che non devono essere confuse con il prodotto base.
_PREMIUM_HINTS = (
    "special",
    "premium",
    "hvo",
    "supreme",
    "hiq",
    "hi-q",
    "vpower",
    "v-power",
    "excellium",
    "shell",
    "dieselmax",
    "blu diesel",
    "blue diesel",
    "blue super",
    "ottani",
    "perform",
    "energy",
    "eco",
    "ecoplus",
    "future",
    "prestazionale",
    "artico",
    "alpino",
    "gelo",
    "igloo",
    "oro",
    "plus",
    "wr 100",
    "f101",
    "f-101",
    "rehvo",
    "bchvo",
    "evo",
    "gp diesel",
    "s-diesel",
    "e-diesel",
)


def normalize_text(value: str) -> str:
    """Normalizza un testo: maiuscole/minuscole, accenti, spazi multipli, punteggiatura."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.upper().strip()
    for ch in "'`´’.,;:-_/\\()[]":
        value = value.replace(ch, " ")
    return " ".join(value.split())


def classify_fuel(descrizione: str) -> str:
    """Mappa una etichetta commerciale (``descCarburante``) su una categoria merceologica."""
    label = normalize_text(descrizione)
    if not label:
        return "Speciali"
    if label == "BENZINA":
        return "Benzina"
    if label == "GASOLIO":
        return "Gasolio"
    if label == "GPL":
        return "GPL"
    if label == "METANO":
        return "Metano"
    if label.startswith("GNL") or label.startswith("LGNC") or label.startswith("L GNC"):
        return "GNL"
    if "METANO" in label and not any(h in label for h in _PREMIUM_HINTS):
        return "Metano"
    if "BENZINA" in label and not any(h in label for h in _PREMIUM_HINTS):
        return "Benzina"
    if "GASOLIO" in label and not any(h in label for h in _PREMIUM_HINTS):
        return "Gasolio"
    return "Speciali"


# --------------------------------------------------------------------------------------
# Tabelle geografiche (regioni ISTAT / codici ISO 3166-2 OSM / sigle province MIMIT)
# --------------------------------------------------------------------------------------
# chiave ISO 3166-2 della regione -> (slug, nome, sigle delle province appartenenti)
REGIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "IT-65": ("abruzzo", "Abruzzo", ("AQ", "CH", "PE", "TE")),
    "IT-77": ("basilicata", "Basilicata", ("MT", "PZ")),
    "IT-78": ("calabria", "Calabria", ("CS", "CZ", "KR", "RC", "VV")),
    "IT-72": ("campania", "Campania", ("AV", "BN", "CE", "NA", "SA")),
    "IT-45": (
        "emilia-romagna",
        "Emilia-Romagna",
        ("BO", "FC", "FE", "MO", "PC", "PR", "RA", "RE", "RN"),
    ),
    "IT-36": ("friuli-venezia-giulia", "Friuli-Venezia Giulia", ("GO", "PN", "TS", "UD")),
    "IT-62": ("lazio", "Lazio", ("FR", "LT", "RI", "RM", "VT")),
    "IT-42": ("liguria", "Liguria", ("GE", "IM", "SP", "SV")),
    "IT-25": (
        "lombardia",
        "Lombardia",
        ("BG", "BS", "CO", "CR", "LC", "LO", "MB", "MI", "MN", "PV", "SO", "VA"),
    ),
    "IT-57": ("marche", "Marche", ("AN", "AP", "FM", "MC", "PU")),
    "IT-67": ("molise", "Molise", ("CB", "IS",)),
    "IT-21": (
        "piemonte",
        "Piemonte",
        ("AL", "AT", "BI", "CN", "NO", "TO", "VB", "VC"),
    ),
    "IT-75": ("puglia", "Puglia", ("BA", "BR", "BT", "FG", "LE", "TA")),
    "IT-88": (
        "sardegna",
        "Sardegna",
        ("CA", "CI", "NU", "OG", "OR", "OT", "SS", "SU", "VS"),
    ),
    "IT-82": ("sicilia", "Sicilia", ("AG", "CL", "CT", "EN", "ME", "PA", "RG", "SR", "TP")),
    "IT-52": (
        "toscana",
        "Toscana",
        ("AR", "FI", "GR", "LI", "LU", "MS", "PI", "PO", "PT", "SI"),
    ),
    "IT-32": ("trentino-alto-adige", "Trentino-Alto Adige", ("BZ", "TN")),
    "IT-55": ("umbria", "Umbria", ("PG", "TR")),
    "IT-23": ("valle-d-aosta", "Valle d'Aosta", ("AO",)),
    "IT-34": ("veneto", "Veneto", ("BL", "PD", "RO", "TV", "VE", "VI", "VR")),
}

#: sigla provincia -> nome esteso (le sigle soppresse restano per i dati storici)
PROVINCES: dict[str, str] = {
    "AG": "Agrigento",
    "AL": "Alessandria",
    "AN": "Ancona",
    "AO": "Aosta",
    "AP": "Ascoli Piceno",
    "AQ": "L'Aquila",
    "AR": "Arezzo",
    "AT": "Asti",
    "AV": "Avellino",
    "BA": "Bari",
    "BG": "Bergamo",
    "BI": "Biella",
    "BL": "Belluno",
    "BN": "Benevento",
    "BO": "Bologna",
    "BR": "Brindisi",
    "BS": "Brescia",
    "BT": "Barletta-Andria-Trani",
    "BZ": "Bolzano",
    "CA": "Cagliari",
    "CB": "Campobasso",
    "CE": "Caserta",
    "CH": "Chieti",
    "CI": "Carbonia-Iglesias",
    "CL": "Caltanissetta",
    "CN": "Cuneo",
    "CO": "Como",
    "CR": "Cremona",
    "CS": "Cosenza",
    "CT": "Catania",
    "CZ": "Catanzaro",
    "EN": "Enna",
    "FC": "Forlì-Cesena",
    "FE": "Ferrara",
    "FG": "Foggia",
    "FI": "Firenze",
    "FM": "Fermo",
    "FR": "Frosinone",
    "GE": "Genova",
    "GO": "Gorizia",
    "GR": "Grosseto",
    "IM": "Imperia",
    "IS": "Isernia",
    "KR": "Crotone",
    "LC": "Lecco",
    "LE": "Lecce",
    "LI": "Livorno",
    "LO": "Lodi",
    "LT": "Latina",
    "LU": "Lucca",
    "MB": "Monza e Brianza",
    "MC": "Macerata",
    "ME": "Messina",
    "MI": "Milano",
    "MN": "Mantova",
    "MO": "Modena",
    "MS": "Massa-Carrara",
    "MT": "Matera",
    "NA": "Napoli",
    "NO": "Novara",
    "NU": "Nuoro",
    "OG": "Ogliastra",
    "OR": "Oristano",
    "OT": "Olbia-Tempio",
    "PA": "Palermo",
    "PC": "Piacenza",
    "PD": "Padova",
    "PE": "Pescara",
    "PG": "Perugia",
    "PI": "Pisa",
    "PN": "Pordenone",
    "PO": "Prato",
    "PR": "Parma",
    "PT": "Pistoia",
    "PU": "Pesaro e Urbino",
    "PV": "Pavia",
    "PZ": "Potenza",
    "RA": "Ravenna",
    "RC": "Reggio Calabria",
    "RE": "Reggio Emilia",
    "RG": "Ragusa",
    "RI": "Rieti",
    "RM": "Roma",
    "RN": "Rimini",
    "RO": "Rovigo",
    "SA": "Salerno",
    "SI": "Siena",
    "SO": "Sondrio",
    "SP": "La Spezia",
    "SR": "Siracusa",
    "SS": "Sassari",
    "SU": "Sud Sardegna",
    "SV": "Savona",
    "TA": "Taranto",
    "TE": "Teramo",
    "TN": "Trento",
    "TO": "Torino",
    "TP": "Trapani",
    "TR": "Terni",
    "TS": "Trieste",
    "TV": "Treviso",
    "UD": "Udine",
    "VA": "Varese",
    "VB": "Verbano-Cusio-Ossola",
    "VC": "Vercelli",
    "VE": "Venezia",
    "VI": "Vicenza",
    "VR": "Verona",
    "VS": "Medio Campidano",
    "VT": "Viterbo",
    "VV": "Vibo Valentia",
}

#: sigla provincia -> slug della regione
PROVINCE_TO_REGION: dict[str, str] = {}
for _region_code, (_slug, _name, _sigle) in REGIONS.items():
    for _sigla in _sigle:
        PROVINCE_TO_REGION[_sigla] = _slug

#: nome regione (slug) -> nome leggibile
REGION_NAMES: dict[str, str] = {slug: name for _code, (slug, name, _s) in REGIONS.items()}
REGION_NAMES["nd"] = "Non geolocalizzati"

#: variazioni ricorrenti nei nomi delle province all'interno dell'anagrafica MIMIT
PROVINCE_ALIASES = {
    "MONZA": "MB",
    "MONZA BRIANZA": "MB",
    "MONZA E BRIANZA": "MB",
    "MONZA E DELLA BRIANZA": "MB",
    "FORLI": "FC",
    "FORLI CESENA": "FC",
    "FORLI E CESENA": "FC",
    "PESARO URBINO": "PU",
    "PESARO E URBINO": "PU",
    "MASSA": "MS",
    "MASSA CARRARA": "MS",
    "BARLETTA": "BT",
    "BARLETTA ANDRIA TRANI": "BT",
    "VERBANO CUSIO OSSOLA": "VB",
    "LA SPEZIA": "SP",
    "AQUILA": "AQ",
    "L AQUILA": "AQ",
    "AOSTA": "AO",
    "VALLE D AOSTA": "AO",
    "SUD SARDEGNA": "SU",
    "CARBONIA IGLESIAS": "CI",
    "MEDIO CAMPIDANO": "VS",
    "OLBIA TEMPIO": "OT",
    "ROMA CAPITALE": "RM",
    "CITTA METROPOLITANA DI ROMA CAPITALE": "RM",
    "MILANO CITTA METROPOLITANA": "MI",
    "NAPOLI CITTA METROPOLITANA": "NA",
    "TORINO CITTA METROPOLITANA": "TO",
    "BOLOGNA CITTA METROPOLITANA": "BO",
    "FIRENZE CITTA METROPOLITANA": "FI",
    "GENOVA CITTA METROPOLITANA": "GE",
    "VENEZIA CITTA METROPOLITANA": "VE",
    "BARI CITTA METROPOLITANA": "BA",
    "CATANIA CITTA METROPOLITANA": "CT",
    "MESSINA CITTA METROPOLITANA": "ME",
    "PALERMO CITTA METROPOLITANA": "PA",
    "CAGLIARI CITTA METROPOLITANA": "CA",
    "REGGIO CALABRIA CITTA METROPOLITANA": "RC",
}


def _build_province_name_index() -> dict[str, str]:
    """Costruisce la mappa nome-normalizzato -> sigla usata per ripulire l'anagrafica."""
    index: dict[str, str] = {}
    for sigla, nome in PROVINCES.items():
        index[normalize_text(nome)] = sigla
    for alias, sigla in PROVINCE_ALIASES.items():
        index[normalize_text(alias)] = sigla
    return index


PROVINCE_NAME_TO_CODE = _build_province_name_index()


def resolve_provincia(value: str) -> str | None:
    """Interpreta il campo ``Provincia`` dell'anagrafica MIMIT (sporco) e restituisce la sigla.

    Il campo contiene nella maggior parte dei casi la sigla a due lettere, ma in alcune
    righe contiene il nome esteso della provincia o addirittura un indirizzo: in quei casi
    si tenta la risoluzione per nome, altrimenti si restituisce ``None`` (verrà risolto
    con l'euristica basata sul comune).
    """
    text = normalize_text(value)
    if not text:
        return None
    compact = text.replace(" ", "")
    if len(compact) == 2 and compact in PROVINCES:
        return compact
    return PROVINCE_NAME_TO_CODE.get(text)
