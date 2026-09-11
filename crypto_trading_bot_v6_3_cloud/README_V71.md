# V7.1 – Fee-Aware Grid 2.0

V7.1 baut direkt auf V7.0 auf. Der Crypto-Radar und alle vorhandenen Paper-/Forward-Tests bleiben erhalten. Neu ist eine strengere zweite Generation des Fee-Aware Grid Challengers.

## Warum dieses Update

Die Tagesauswertung vom 10.09.2026 zeigte, dass Gebühren einen großen Teil des bisherigen Ergebnisses auffressen. Besonders beim BTC-/ETH-Grid waren die Bruttoergebnisse deutlich besser als die Nettoergebnisse. V7.1 reduziert deshalb gezielt unnötige Rebalances.

## Neu in V7.1

- Mindest-Edge für neue Exposition: mindestens **3× reine Roundtrip-Gebühr** oder, falls höher, die All-in-Hürde aus Gebühren + angenommener Slippage + Sicherheitspuffer.
- Dynamischer Grid-Abstand: standardmäßig **0,80–1,50 %**, abhängig von der rollierenden 24h-Stundenvolatilität.
- Cooldown: standardmäßig **4 Stunden** zwischen normalen Rebalances.
- Signalbestätigung: weiterhin **2 abgeschlossene Stunden**.
- Exposure-Bremse: maximal **60 %** investiert; im Bear-Regime maximal **40 %**.
- Starker Risk-off-Ausstieg wird weiterhin nicht verzögert.
- Zusätzliche Blocker-Zähler im Fee-Grid-Tab: Kosten, Abstand und Exposure.
- Verkaufs-P/L wird transparenter: V6-Trade-Events führen jetzt den gewichteten Einstand und den Brutto-G/V mit. So ist nachvollziehbar, warum ein Verkauf trotz eines höheren Kurses als beim zuletzt sichtbaren Kauf negativ sein kann.
- Eine bestehende Fee-Grid-Saison kann über **„Saubere V7.1-Saison neu starten“** neu begonnen werden. Der alte Fee-Grid-State/Verlauf wird vorher unter `/data/fee_grid_archive/` gesichert.

## Standardwerte

- Startkapital: 1.000 € je Coin und Variante
- Gebühr: aus dem vorhandenen V6-State, typisch 0,25 % je Order
- Slippage-Annahme: 0,10 % je Order
- Sicherheitspuffer: 0,35 %
- Fee-Multiplikator: 3,0
- Mindestabstand: 0,80 %
- Maximalabstand: 1,50 %
- Volatilitätsmultiplikator: 1,80
- Bestätigung: 2 Stunden
- Cooldown: 4 Stunden
- Max. Exposure normal: 60 %
- Max. Exposure Bear-Regime: 40 %

Bei 0,25 % Gebühr je Order ergibt sich standardmäßig eine Mindest-Edge von 1,50 % (3 × 0,50 % Roundtrip-Gebühr). Die alternative All-in-Hürde beträgt 1,05 %, daher greift in diesem Beispiel die strengere 1,50-%-Schwelle.

## Update auf dem bestehenden Railway-Projekt

Diese Dateien aus dem Patch im GitHub-Hauptverzeichnis ersetzen:

- `fee_aware_grid.py`
- `experiments.py`
- `cloud_core.py`
- `notifications.py`
- `cloud_app.py`

Danach committen/pushen. Railway deployt automatisch. **Das persistente `/data`-Volume nicht löschen.**

Nach dem Deployment den Tab **💸 Fee-Grid** öffnen. Für einen sauberen V7.1-vs-Standard-Vergleich eine neue V7.1-Saison starten. Der alte Fee-Grid-Stand wird archiviert.

## Wichtig

Weiterhin ausschließlich Paper-/Shadow-Trading. V7.1 enthält keinen Broker-Login, keine API-Schlüssel für Trading und keine Funktion für Echtgeld-Orders.
