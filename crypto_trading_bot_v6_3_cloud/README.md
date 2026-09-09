# Crypto Trading Zentrale – V6.7 Strategy Challenger

V6.7 ergänzt die bestehende Cloud-Zentrale um einen **Strategy Challenger**. Neue Strategien laufen ausschließlich im Shadow-/Paper-Modus gegen einen eingefrorenen Champion. Die bestehenden V5.2-/V6-Forward-Tests, das Signal-Labor, Telegram und das Railway-Volume `/data` bleiben erhalten.

## Neu in V6.7

Im neuen Tab **🏆 Strategy Challenger** kannst du eine neue Forward-Saison starten. Beim Start werden festgeschrieben:

- Champion
- Startzeitpunkt
- Coin-Universum
- Gebühren
- Regeln der Challenger
- Promotion-Kriterien

Bereits bekannte Kurse vor dem Start zählen nicht als Challenger-Trades.

### Erste Shadow-Challenger

**Quality 10/10 Strict**
- Einstieg nur bei vollständigem 10/10-Quality-Setup
- Edge mindestens 85
- ein Coin gleichzeitig
- 20 % Positionsgröße
- Stop-Loss 4 %, Take-Profit 8 %
- Trailing-Stop 3 % ab +5 %

**Quality 9/10 Confirmed**
- mindestens 9/10 Regeln
- Edge mindestens 90
- derselbe Kandidat muss zwei aufeinanderfolgende Cloud-Scans bestätigen
- identische Risiko-/Exit-Grundregeln

Beide Strategien verwenden nur neue Markt-Scans nach dem Challenge-Start.

## Promotion-Regeln

Eine Strategie bekommt erst **🏆 PROMOTION EMPFOHLEN**, wenn sie gleichzeitig:

- mindestens 30 Tage Forward gelaufen ist
- mindestens 8 abgeschlossene Trades hat
- netto positiv ist
- mindestens 2 Prozentpunkte vor dem Champion liegt
- Profit Factor mindestens 1,20 erreicht
- maximal 10 % Drawdown hat
- alle Kriterien anschließend 72 Stunden ohne Unterbrechung erfüllt

**Wichtig:** Es gibt keine automatische Umschaltung. Eine Promotion ist nur eine Empfehlung im Dashboard und per Telegram. Der Champion bleibt unverändert, bis du bewusst entscheidest.

## Railway-Update von V6.6

Für die bestehende Cloud-App reicht der Patch. Lade diese drei Dateien in das **Hauptverzeichnis** des GitHub-Repositories und ersetze vorhandene Dateien:

- `strategy_challenger.py` (neu)
- `worker.py`
- `cloud_app.py`

Danach Commit. Railway deployt automatisch neu.

**Das Railway-Volume `/data` nicht löschen.** Dort liegen deine bestehenden Forward-States, Telegram-Chat-ID, Signal-Labor-Daten und künftig auch der Strategy-Challenger-State.

Es sind keine neuen Railway-Variablen nötig.

## Start der ersten Challenger-Saison

Nach dem Deployment:

1. Cloud-Zentrale öffnen.
2. Tab **🏆 Strategy Challenger** öffnen.
3. Champion prüfen (standardmäßig `Quality Breakout`, falls verfügbar).
4. Auf **Challenger-Saison ab jetzt einfrieren** drücken.
5. Danach nichts an den eingefrorenen Regeln ändern.

Der Cloud-Worker aktualisiert die Challenger anschließend automatisch mit jedem Markt-Scan. Wenn ein Challenger die Promotion-Regeln erfüllt, sendet die vorhandene Telegram-Verbindung eine Meldung.

## Neue Dateien im Railway-Volume

- `/data/strategy_challenger.json`
- `/data/strategy_challenger_history.csv`

## Sicherheit

V6.7 bleibt reines Paper-/Shadow-Trading. Es gibt weiterhin **keine echte Broker-Order-Ausführung** und keine automatische Promotion in Live-Trading.
