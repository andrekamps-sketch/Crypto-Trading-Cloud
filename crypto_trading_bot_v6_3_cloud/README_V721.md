# Crypto Trading Zentrale V7.2.1 – Candlestick Patch

Änderungen gegenüber V7.2:

- maximal **eine offene Candlestick-Position je Coin**, auch wenn 15m und 1h gleichzeitig Signale liefern
- bei gleichgerichteten bestätigten Signalen auf 15m + 1h wird nur das stärkste Setup gehandelt und erhält **+5 Multi-Timeframe-Konfluenzpunkte** (max. Score 100)
- alle Signale desselben Coins aus demselben Scan werden beim Einstieg als verarbeitet markiert, damit kein zweiter veralteter Timeframe-Trade nachrutscht
- Telegram-Einstiegsmeldungen zeigen **Einstieg, Stop-Loss, Ziel, CRV, Paper-Betrag und Multi-TF-Konfluenz**
- Telegram-Ausstiegsmeldungen zeigen Entry → Exit und Nettoergebnis
- bestehende offene Doppelpositionen aus V7.2 werden beim Update **nicht zwangsweise geschlossen**; sie werden normal bis Stop/Ziel weitergeführt. Neue Doppelpositionen entstehen nicht mehr.

Nur Paper-Trading. Keine echten Orders.
