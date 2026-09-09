# V6.10 – Fee-Aware Grid Challenger

V6.10 ergänzt die bestehende Cloud-Zentrale um einen separaten Shadow-Vergleich zwischen dem bisherigen BTC-/ETH-Adaptive-Grid und einer gebührenbewussteren Variante. Die bestehenden V5.2-, V6-, Signal-Labor-, Strategy-Challenger- und Live-Paper-States werden nicht verändert.

## Idee

Beide Varianten starten ab demselben neuen Zeitpunkt mit demselben Spielgeld. Das Standard-Grid nutzt die bisherige V6-Logik. Fee-Aware nutzt dieselben z-Score-Zielstufen, blockiert aber kleine bzw. zu schnelle Rebalances und verlangt für neue Exposition eine erwartete Rücklauf-Strecke, die Hin-/Rückgebühren, angenommene Slippage und einen Sicherheitspuffer übersteigt.

Standardwerte:
- 1.000 € Spielgeld je Coin und Variante
- BTC und ETH
- Gebühr aus dem vorhandenen V6-State (typisch 0,25 % je Order)
- angenommene Slippage 0,10 % je Order
- Sicherheitspuffer 0,35 %
- 2 abgeschlossene Stunden Bestätigung
- mindestens 3 Stunden zwischen normalen Rebalances
- mindestens 0,70 % Kursweg seit dem letzten Rebalance
- Risk-off-Ausstiege werden nicht verzögert

Im Tab **💸 Fee-Grid** werden Kontowert, Rendite, Drawdown, Profit Factor, Trades, Gebühren, Gebührenersparnis und der Netto-Abstand zum Standard-Grid angezeigt.

## Installation als Patch

Diese drei Dateien ins Hauptverzeichnis des bestehenden GitHub-Repositories hochladen/ersetzen:
- `fee_aware_grid.py` (neu)
- `worker.py`
- `cloud_app.py`

Danach Railway automatisch neu deployen lassen. Das persistente `/data`-Volume nicht löschen.

Nach dem Deployment im Tab **💸 Fee-Grid** einmal **Fee-Aware-vs-Standard ab jetzt starten** drücken. Erst ab diesem Startpunkt zählt der Vergleich.

## Wichtig

Das ist ausschließlich Paper-/Shadow-Trading. Kein Broker-Login, keine echten Orders. Ein besseres Ergebnis in kurzen Zeiträumen ist kein Beweis für einen nachhaltigen Vorteil; der Vergleich soll über genügend Trades und mehrere Marktphasen laufen.
