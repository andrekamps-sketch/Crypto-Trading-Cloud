# V6.9 – Multi-Coin Live-Paper Long + Short

V6.9 ergänzt V6.8 um einen **separaten Long+Short-Paper-Challenger**. Der bisherige Long-only-Live-Paper-Test bleibt unverändert und nutzt weiterhin seine eigene State-Datei.

## Neu

- neuer Tab `↕️ Long+Short`
- dieselben 12 Coins wie im Multi-Coin-Monitor
- LONG und SHORT können gleichzeitig gehandelt werden
- max. 3 Positionen und standardmäßig 60 % Gesamt-Exposure
- SHORT wird **ohne Hebel** simuliert: 1:1 Spielgeld wird als Sicherheit reserviert
- Gebühren + Slippage + konservative Funding-Kosten für SHORT
- LONG: Quality-Regeln/Edge wie bisher
- SHORT: eigener bearish Checklist-Score aus Trend, Momentum, RSI, 24h/7T-Rendite und Long-Score
- BULL-Regime bremst neue SHORTs; BEAR-Regime bremst neue LONGs; NEUTRAL erlaubt beide
- Stop-Loss, Take-Profit und Trailing-Stop für beide Richtungen
- getrennte Telegram-Meldungen für LONG/SHORT Einstieg und Exit
- eigener Kontoverlauf und Trade-CSV

## Standardwerte

- Startkapital: 1.000 € Spielgeld
- Position: 20 %
- max. 3 Positionen
- max. Exposure: 60 %
- Stop: 4 %
- Ziel: 8 %
- Trailing: 3 % ab +4 %
- Gebühr: 0,25 %
- Slippage: 0,10 %
- SHORT Funding: 0,01 % je 8h (konservative Paper-Annahme)

## Wichtig

Das ist ausschließlich Paper-Trading. Es gibt **keinen Broker-Login, keinen API-Key und keine echte Orderfunktion**. Reales Shorting erfordert typischerweise Margin/Derivate und ist nicht dasselbe wie ein normaler Spot-Kauf.
