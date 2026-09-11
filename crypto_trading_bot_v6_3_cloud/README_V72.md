# Crypto Trading Zentrale V7.2 – Candlestick V2 Quality Filter

V7.2 baut auf V7.1 auf und ergänzt einen neuen Candlestick-Paper-Bot mit strengen Qualitätsregeln.

## Neue Schutzregeln
- Nur eine Candlestick-Position je Coin über alle Timeframes.
- 15m-Signale nur mit bestätigtem 1h-Trend; 1h-Signale nur mit bestätigtem 4h-Trend.
- Kerzenmuster + EMA20/EMA50 + Higher-Timeframe + RSI/Volumen ergeben einen Quality-Score; Einstieg erst ab 6/8.
- 6 Stunden Cooldown nach Stop-Loss im selben Coin.
- ATR-basierter Stop, Take-Profit 2R, Break-even-Schutz nach +1R.
- Verlustbudget standardmäßig ca. 2,50 € je Trade bei 1.000 € Testkonto einschließlich angenäherter Gebühren.
- Volatile/kleinere Märkte bekommen nur 50% Risikobudget, mittlere 75%.
- Maximal 3 offene Candlestick-Positionen und max. 20% Notional je Trade.

## Wichtig
V7.2 ist ausschließlich Paper-Trading. Es gibt keinen Broker-Login und keine echten Orders.

Nach dem Deploy im Dashboard den Tab **🕯️ Candlestick V2** öffnen und den neuen Test starten. Alte Candlestick-State-Dateien werden nicht gelöscht.
