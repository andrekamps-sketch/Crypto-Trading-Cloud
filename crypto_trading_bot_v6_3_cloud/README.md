# Crypto Trading Zentrale – V6.6 Signal-Labor

V6.6 ergänzt die bestehende Cloud-Zentrale um ein **rein beobachtendes Quality-Signal-Labor**. Die eingefrorenen V5.2-/V6-Regeln, Paper-Positionen, Telegram-Einstellungen und das Railway-Volume `/data` werden nicht verändert.

## Neu in V6.6

- speichert 9/10- und 10/10-Quality-Signale automatisch
- speichert Signalpreis, Edge, RSI, Marktphase und fehlende Regel
- misst den Kurs danach automatisch bei ungefähr:
  - 1 Stunde
  - 6 Stunden
  - 24 Stunden
  - 3 Tagen
  - 7 Tagen
- führt pro Signal den bisher beobachteten maximalen Anstieg und Rückgang mit
- trennt 9/10 und 10/10 in der Statistik
- zeigt Trefferquote, Durchschnitt, Median, bestes und schlechtestes Ergebnis je Horizont
- zeigt eine Coin-Auswertung nach 24 Stunden, sobald genügend Daten vorhanden sind
- CSV-Export aller Signal-Labor-Daten

## Verhalten beim ersten V6.6-Lauf

Coins, die beim Update bereits auf 9/10 oder 10/10 stehen, werden einmalig als `baseline` gespeichert. Dadurch kann ihre weitere Kursentwicklung gemessen werden, ohne so zu tun, als wäre das Signal erst durch V6.6 neu entstanden. Spätere echte Übergänge werden als `transition` gespeichert.

## Update der bestehenden Railway-App

Für ein bestehendes V6.5.2-System reicht der kleine Patch. Lade diese drei Dateien ins **Hauptverzeichnis** des GitHub-Repositories und ersetze die vorhandenen Dateien:

- `signal_lab.py` (neu)
- `worker.py`
- `cloud_app.py`

Danach Commit. Railway deployt automatisch neu.

**Nicht löschen:** das Railway-Volume mit Mount Path `/data`. Dort liegen die laufenden Forward-Zustände, Telegram-Chat-ID und künftig auch die Signal-Labor-Daten.

Es sind **keine neuen Railway-Variablen** nötig.

## Wo finde ich das Labor?

In der Cloud-Zentrale gibt es nach dem Update den neuen Tab **🧪 Signal-Labor**. Der Worker aktualisiert es bei jedem automatischen Markt-Scan. Manuell kann der zuletzt gespeicherte Scan dort ebenfalls übernommen werden.

## Dateien im Volume

- `/data/signal_lab_events.json`
- `/data/signal_lab_state.json`

## Wichtig

Die Statistik ist am Anfang zwangsläufig dünn. Ein einzelnes gutes 9/10-Signal beweist keinen Vorteil. Interessant wird das Labor erst mit einer größeren Zahl unabhängiger Signale über unterschiedliche Marktphasen.

V6.6 bleibt reines Paper-Trading/Monitoring und enthält keine echte Broker-Order-Ausführung.
