# Crypto Trading Zentrale – V6.4 Cloud

V6.4 ist die 24/7-Cloud-/Handy-Zentrale für die eingefrorenen Paper-Tests aus V5.2 und V6. Sie verändert die bestehenden Regeln und Startzeitpunkte nicht und enthält weiterhin **keine echte Broker-Order-Funktion**.

## Neu in V6.4

- automatische Paper-Auswertung über den bestehenden Worker (Standard: jede Stunde)
- Telegram-Benachrichtigungen bei neuen Paper-Trade-Aktionen
- Telegram-Hinweis, wenn ein Markt-Kandidat neu mindestens 9/10 Quality-Regeln erfüllt
- Telegram-Hinweis bei Führungswechsel im Paper-Wettkampf
- tägliche Telegram-Zusammenfassung, standardmäßig ab 20:00 Uhr Europe/Berlin
- Telegram-Einrichtung direkt im Dashboard: Chat-ID automatisch erkennen und Testnachricht senden
- persistent gespeicherter Benachrichtigungszustand unter `/data`, sodass keine alten Meldungen nach jedem Redeploy erneut versendet werden

## Update von V6.3 auf V6.4 in Railway

Das bestehende Railway-Volume mit Mount Path `/data` bleibt kompatibel und darf nicht gelöscht werden. Dadurch bleiben die importierten V5.2-/V6-States, Zwischenstände und Verläufe erhalten.

1. V6.4 entpacken.
2. Den **Inhalt** des entpackten Ordners in das Hauptverzeichnis deines bestehenden GitHub-Repositories hochladen und vorhandene Dateien ersetzen.
3. Committen. Railway startet anschließend automatisch einen neuen Deploy.
4. Prüfen, dass das bestehende Volume weiterhin an `/data` gemountet ist.

## Telegram einrichten

1. In Telegram `@BotFather` öffnen.
2. `/newbot` senden und den Anweisungen folgen.
3. Den erzeugten Bot-Token kopieren.
4. In Railway beim Crypto-Trading-Service unter **Variables** hinzufügen:

```text
TELEGRAM_BOT_TOKEN=<dein Bot-Token>
```

5. Die Änderung deployen.
6. Den eigenen neuen Bot in Telegram öffnen, **Start** drücken und z. B. `Hallo` senden.
7. In V6.4 den Tab **🔔 Benachrichtigungen** öffnen und **Chat-ID automatisch finden & speichern** anklicken.
8. Anschließend **Testnachricht senden**.

Die Chat-ID wird im persistenten `/data`-Volume gespeichert. Alternativ kann sie als Railway-Variable `TELEGRAM_CHAT_ID` gesetzt werden.

## Optionale Railway-Variablen

```text
AUTO_UPDATE_MINUTES=60
AUTO_MARKET_SCAN=1
WORKER_ENABLED=1
DATA_DIR=/data

NOTIFY_TRADES=1
NOTIFY_MARKET_CANDIDATES=1
NOTIFY_MARKET_MIN_RULES=9
NOTIFY_LEADER_CHANGE=1
NOTIFY_DAILY_SUMMARY=1
DAILY_SUMMARY_HOUR=20
NOTIFY_TIMEZONE=Europe/Berlin
```

`AUTO_UPDATE_MINUTES=60` bedeutet: der Cloud-Worker wertet die Paper-Tests und den Markt-Monitor ungefähr stündlich neu aus. Die höchste sinnvoll unterstützte Aktualisierungsfrequenz für dieses Projekt ist nicht als Hochfrequenz-Trading gedacht; die Strategien selbst basieren ohnehin auf 1h-/6h-Daten.

## Benachrichtigungslogik

Beim ersten Lauf nach dem Upgrade setzt V6.4 einen Baseline-Zustand. Bereits vergangene Trades werden **nicht** rückwirkend als neue Telegram-Alarme versendet. Erst danach neu auftretende Trade-Aktionen oder neu erreichte Markt-Schwellen werden gemeldet.

## Sicherheit

- `TRADING_DASHBOARD_PASSWORD` als Railway-Secret/Variable setzen.
- `TELEGRAM_BOT_TOKEN` ausschließlich als Railway-Variable speichern, niemals in GitHub committen.
- Keine Broker-API-Schlüssel in Repository oder ZIP-Dateien ablegen.
- V6.4 ist weiterhin Paper-Trading/Monitoring und kann keine echten Orders senden.
