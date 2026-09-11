# V7.0 – Crypto Opportunity Radar

V7.0 ergänzt die bestehende Crypto-Trading-Cloud um einen **breiten Markt-Radar**. Die eingefrorenen Forward-Tests, V5.2/V6, Live-Paper, Long+Short, Signal-Labor, Strategy Challenger und Fee-Aware Grid bleiben getrennt bestehen.

## Was der Radar macht

- nutzt öffentliche Binance-Spot-Marktdaten ohne Trading-API-Key
- betrachtet das aktuelle Universum handelbarer **USDT-Spotmärkte**
- filtert standardmäßig illiquide Märkte unter 2 Mio. USD 24h-Volumen aus
- wählt aus dem breiten Universum auffällige/liquide Märkte für eine tiefere 1h-Analyse
- Detailanalyse standardmäßig 40 Coins pro Scan, manuell 20–80 einstellbar
- bewertet u. a. 1h/6h/24h/7T-Momentum, Volumen-Spike, RSI, EMA20/50-Trend, Volatilität, Nähe zu 7-Tage-Hoch/Tief und Liquidität
- erstellt einen **Radar-Score 0–100** und einen getrennten **Anomalie-Score**
- markiert Risikofaktoren wie geringe Liquidität, extreme 24h-Bewegung, extremes RSI und sehr hohe Volatilität
- speichert Top-20 jedes Scans im persistenten `/data`-Volume für spätere Auswertung
- kann Telegram nur bei **neuem Überschreiten** einer Radar-/Anomalie-Schwelle informieren; der erste Scan ist absichtlich eine stille Baseline

## Wichtige Abgrenzung

Der Radar ist ein Discovery-/Beobachtungssystem. Ein hoher Score ist **keine Gewinnprognose** und löst **keine echte Order** aus. V7.0 hat weiterhin keinen Broker-Login und keine Funktion für Echtgeld-Orders.

"Alle Coins" bedeutet hier: das jeweils von der öffentlichen Datenquelle gelistete, handelbare Binance-USDT-Spotuniversum. Es gibt keinen einzelnen Anbieter, der buchstäblich jeden Token auf jeder Blockchain abdeckt.

## Installation auf bestehendem Railway-Projekt

Für das Update reichen diese 3 Dateien im GitHub-Hauptverzeichnis:

- `crypto_radar.py` (neu)
- `worker.py` (ersetzen)
- `cloud_app.py` (ersetzen)

Danach committen. Railway deployt automatisch. Das bestehende `/data`-Volume **nicht löschen**.

Keine neue Railway-Variable ist zwingend nötig. Der Radar läuft standardmäßig im normalen Worker-Zyklus (typisch stündlich). Optional kann er mit `CRYPTO_RADAR_ENABLED=0` abgeschaltet werden.

## Bedienung

Nach dem Deployment gibt es den neuen Tab **🌐 Crypto-Radar**. Dort kann ein Scan manuell gestartet werden. Der Worker aktualisiert ihn zusätzlich automatisch.
