# V6.9 Long+Short Patch

In GitHub im Hauptverzeichnis von `Crypto-Trading-Cloud` diese drei Python-Dateien hochladen bzw. ersetzen:

- `live_paper_long_short.py` (neu)
- `worker.py`
- `cloud_app.py`

Danach `Commit changes`. Railway deployt automatisch.

Keine neue Railway-Variable ist nötig. Das `/data`-Volume **nicht löschen**.

Der bestehende V6.8 Long-only-Live-Paper-State bleibt getrennt erhalten. V6.9 legt für Long+Short eigene Dateien unter `/data/live_paper_long_short_*` an.
