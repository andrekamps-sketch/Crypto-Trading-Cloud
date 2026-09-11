# V6.8 – Multi-Coin Live-Paper Patch

Dieses Update ergänzt die bestehende Railway-Cloud-Zentrale um einen separaten **Multi-Coin Live-Paper-Modus**. Es werden aktuelle Marktpreise verwendet, aber ausschließlich virtuelles Spielgeld. Es gibt weiterhin **keinerlei echte Broker-/Bitpanda-Orderfunktion**.

## GitHub: diese 4 Dateien ins Repository-Hauptverzeichnis hochladen

- `live_paper.py` – neu
- `market_data.py` – ersetzt die vorhandene Datei
- `worker.py` – ersetzt die vorhandene Datei
- `cloud_app.py` – ersetzt die vorhandene Datei

Danach Commit. Railway deployt automatisch. Das bestehende `/data`-Volume **nicht löschen**.

## Neue Funktion

Tab **🎮 Live-Paper**:

- Standard 1.000 € Spielgeld
- 12 Coins: BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, LINK, LTC, DOT, BCH
- aktuelle 5-Minuten-Kurse für Mark-to-Market und virtuelle Ausführung
- Quality-Signale aus dem bestehenden 1h-Markt-Monitor
- Ranking der Coins; stärkste passende Kandidaten zuerst
- standardmäßig max. 3 offene Positionen
- 20% je Position, max. 60% gleichzeitig investiert
- Stop-Loss 4%, Take-Profit 8%
- Trailing-Stop 3% ab +4%
- simulierte Gebühr 0,25% und Slippage 0,10%
- Cooldown nach Exit 6h
- Telegram-Meldung bei Live-Paper-Kauf/Verkauf
- Kontoverlauf und Trade-CSV

Die Live-Paper-Positionen werden unabhängig von V5.2, V6, Signal-Labor und Strategy Challenger gespeichert.

## Aktualisierung

Der vorhandene Worker führt die normale Vollauswertung weiterhin standardmäßig alle 60 Minuten aus. Der Live-Paper-Teil prüft, sobald er gestartet wurde, standardmäßig alle **5 Minuten** die neuesten Preise. Die 12 Coins werden soweit möglich in einem einzigen Kursdaten-Batch geladen.

Optional kann in Railway gesetzt werden:

`LIVE_PAPER_UPDATE_MINUTES=5`

Der Standard ist bereits 5 Minuten; eine neue Variable ist daher nicht nötig.

## Hinweis zur Kursquelle

Die 5-Minuten-Kurse stammen über `yfinance` von Yahoo Finance. Sie sind für Monitoring/Paper-Trading gedacht und nicht als exchange-grade Orderbuch-/Ausführungskurse zu verstehen. Gebühren und Slippage werden deshalb zusätzlich simuliert.
