"""mimit_fuel — report sugli andamenti dei prezzi dei carburanti dagli open data MIMIT.

Moduli principali:

* :mod:`mimit_fuel.sources` — scoperta e download degli archivi trimestrali MIMIT
* :mod:`mimit_fuel.parsing` — lettura/normalizzazione dei CSV (anagrafica e prezzi)
* :mod:`mimit_fuel.aggregate` — aggregazione giornaliera per provincia e comune
* :mod:`mimit_fuel.geo` — confini amministrativi da OpenStreetMap
* :mod:`mimit_fuel.timeseries` — serie storiche e payload del report
* :mod:`mimit_fuel.report` — generazione del report HTML
* :mod:`mimit_fuel.pipeline` — orchestrazione completa
"""

__version__ = "1.0.0"
