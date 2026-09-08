# V6.5.1 – Gebühren-/Netto-Patch

Dieser kleine Patch erweitert nur die tägliche Telegram-Zusammenfassung. Die eingefrorenen V5.2-/V6-Regeln, Startzeitpunkte, Telegram-Einstellungen und das Railway-Volume `/data` bleiben unverändert.

## Neu

Die Tagesübersicht zeigt zusätzlich eine klare Gesamtzeile für alle aktuell bewertbaren Paper-Konten:

```text
💰 Konten-P/L seit Start: Brutto +9.80 € · Gebühren -18.11 € · Netto -8.31 €
```

Dabei gilt:
- **Netto** = aktueller Liquidations-/Kontowert minus eingefrorenes Startkapital der aktuell bewertbaren Strategien.
- **Gebühren** = die in denselben Kontowerten bereits berücksichtigten simulierten Gebühren.
- **Brutto** = Netto + Gebühren, also das Portfolio-Ergebnis vor diesen Gebühren.

Damit wird nicht nur die Summe gewonnener Verkäufe gezeigt. Offene Positionen und deren aktuelle Kursentwicklung sind im Konten-P/L ebenfalls enthalten. Das macht die Zahl mit den im Dashboard gezeigten Kontowerten konsistent.

## Installation

Die vier Dateien aus diesem ZIP in das **Hauptverzeichnis** des bestehenden GitHub-Repositories `Crypto-Trading-Cloud` hochladen und vorhandene Dateien ersetzen:

- `notifications.py`
- `bridge_v52.py`
- `cloud_core.py`
- `README.md` (optional)

Danach `Commit changes`. Railway deployt automatisch neu.

**Nicht löschen:**
- Railway-Volume `crypto-trading-cloud-volume`
- Mount Path `/data`
- vorhandene Variables / Telegram-Token / Passwort

Nach dem Deployment einmal in der Zentrale **„Jetzt mit neuen Kursdaten auswerten“** drücken. Dadurch werden die Startkapitalwerte in den aktuellen V5.2-/V6-Auswertungsdateien ergänzt. Danach enthält auch **„Tagesübersicht jetzt senden“** die neue Brutto/Gebühren/Netto-Zeile.
