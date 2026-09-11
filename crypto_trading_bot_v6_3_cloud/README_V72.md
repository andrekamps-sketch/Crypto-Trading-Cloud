# V7.2 – Coin Candlestick Scanner

V7.2 erweitert die bestehende Cloud-Trading-Zentrale um eine eigenständige Paper-Strategie `Coin Candlestick Scanner`.

## Was neu ist
- dynamisches Universum: Top 50 liquide Binance-USDT-Spotmärkte
- Mindestliquidität: 5 Mio. USDT 24h-Quote-Volumen
- Spread-Filter: maximal 25 Basispunkte
- Zeitrahmen: 15 Minuten und 1 Stunde
- Muster: Bullish/Bearish Engulfing, Hammer, Shooting Star, Bullish/Bearish Pin Bar, Morning Star, Evening Star
- nur abgeschlossene Kerzen; Einstieg erst nach Bestätigung durch die folgende abgeschlossene Kerze
- Score 0–100: Muster 20, Trend 20, Support/Widerstand 20, Volumen 15, RSI/Momentum 10, Bestätigung 10, CRV 5
- Standard-Einstieg ab Score 75
- virtuelles Startkapital 1.000 EUR
- Risiko pro Trade 0,75 Prozent
- maximal 3 parallele Positionen, maximal 30 Prozent Paper-Notional pro Position und 75 Prozent Gesamt-Exposure
- ATR-/Signal-Kerzen-Stop und 2:1 Chance/Risiko-Ziel
- simulierte Kosten: 0,10 Prozent Gebühr je Ausführung plus 0,05 Prozent Slippage
- LONG und 1x-Paper-SHORT ohne Hebel und ohne echte Orders
- Telegram-Hinweise bei simuliertem Einstieg/Ausstieg
- Dashboard-Tab mit Signalen, offenen Positionen, Trades, Kontoverlauf und Auswertung nach Muster/Zeitrahmen/Richtung
- Aufnahme in die zentrale Strategie-Rangliste

## Automatischer Start
Ist noch kein Candlestick-State in `/data` vorhanden, startet der Worker die neue Paper-Strategie automatisch mit 1.000 EUR. Das lässt sich mit `CANDLESTICK_AUTO_START=0` abschalten.

## Daten und Sicherheit
Der Scanner nutzt nur öffentliche Binance-Marktdaten. Es werden keine Binance-API-Keys, Broker-Zugangsdaten oder Order-Berechtigungen benötigt. Alle Trades sind Simulationen. USDT-Kursbewegungen werden prozentual auf das virtuelle EUR-Notional übertragen; es findet keine echte EUR/USDT-Konvertierung statt.

## Dateien
- `candlestick_scanner.py`: Scanner, Scoring, Positions- und Risiko-Logik
- `worker.py`: automatischer 5-Minuten-Zyklus; tiefer Candlestick-Scan maximal alle 15 Minuten
- `cloud_app.py`: neuer Tab `🕯️ Candlestick`
- `cloud_core.py`: Candlestick-Strategie in der zentralen Rangliste
