# Crypto Trading Zentrale V6.3 Cloud

V6.3 ist die Cloud-/Handy-Version der bisherigen Trading-Zentrale. Sie bleibt **reines Paper-Trading und Monitoring**. Es gibt keine Funktion zum Senden echter Broker-Orders.

## Was ist neu?

- 24/7-fähiger Cloud-Betrieb in einem Docker-Container
- Passwortschutz über `TRADING_DASHBOARD_PASSWORD`
- persistenter Datenordner über `DATA_DIR` (für Cloud am besten `/data`)
- stündlicher Hintergrund-Worker (Intervall über `AUTO_UPDATE_MINUTES`)
- V5.2 Forward-Wettkampf und V6 Pairs/Grid gemeinsam
- Markt-Monitor mit automatischer Aktualisierung
- mobile Kurzansicht fürs Smartphone
- Import der eingefrorenen V5.2-/V6-State-Dateien direkt im Browser
- Verlauf/CSV-Export

## Bestehende Forward-Tests übernehmen

Die zwei wichtigen Dateien auf deinem PC sind:

- `crypto_trading_bot_v5_2/forward_competition.json`
- `crypto_trading_bot_v6/v6_forward_state.json`

Sie enthalten die **eingefrorenen Startzeitpunkte und Regeln**. V6.3 verändert diese Regeln nicht.

Wenn V5.2, V6 und V6.3 nebeneinander liegen, kannst du lokal `import_local_states.bat` starten. Das kopiert die beiden Dateien nur in den V6.3-Datenordner; die Originale bleiben unverändert.

Alternativ kannst du die zwei JSON-Dateien nach der Cloud-Bereitstellung im Tab **Cloud-Setup** hochladen.

## Lokal testen

1. ZIP entpacken.
2. Optional `import_local_states.bat` ausführen.
3. `start_local_test.bat` starten.
4. Browser: `http://localhost:8508`

Für den lokalen Test ist noch kein Cloud-Server nötig.

## Cloud-Betrieb

Der Ordner enthält einen `Dockerfile`. Für praktisch jeden Docker-fähigen Hoster gelten dieselben Punkte:

- Anwendung aus dem Dockerfile bauen.
- Persistenten Datenspeicher nach `/data` mounten.
- `TRADING_DASHBOARD_PASSWORD` als geheime Umgebungsvariable setzen.
- `DATA_DIR=/data` setzen.
- Der Hoster muss den bereitgestellten `PORT` an den Container weiterreichen (das Startskript übernimmt ihn automatisch).

Empfohlene Umgebungsvariablen:

```text
TRADING_DASHBOARD_PASSWORD=<langes privates Passwort>
AUTO_UPDATE_MINUTES=60
AUTO_MARKET_SCAN=1
WORKER_ENABLED=1
DATA_DIR=/data
```

Wichtig: Wenn der Hoster den Dienst bei Inaktivität schlafen legt, läuft auch der stündliche Worker während dieser Schlafzeit nicht. Für echte 24/7-Aktualisierung braucht der Dienst einen **Always-on/24-7-Modus**. Beim späteren Öffnen kann die Zentrale die Paper-Ergebnisse trotzdem mit den inzwischen verfügbaren Kursen nachrechnen.

## Sicherheit

- Keine API-Schlüssel in ZIP-Dateien, Git-Repositories oder Python-Dateien eintragen.
- Für die öffentliche Cloud immer ein starkes Dashboard-Passwort setzen.
- Spätere Bitpanda-Anbindung zuerst nur mit Leserechten und als Server-Secret.
- V6.3 hat aktuell bewusst **keine Live-Order-Funktion**.

## Dateien

- `cloud_app.py` – mobile Cloud-Oberfläche
- `worker.py` – automatischer Hintergrundlauf
- `cloud_core.py` – State, Auswertung, Speicherung
- `engines/v52/` – eingefrorene V5.2-Auswertungslogik
- `market_data.py`, `experiments.py`, `monitor.py` – V6/Monitor-Logik
- `Dockerfile` / `start_cloud.sh` – Cloud-Start
- `docker-compose.yml` – optional für eigenen Server/NAS
- `import_local_states.bat` – übernimmt die bestehenden Forward-Startdateien lokal

## Hinweis

Backtests und Paper-Trading können reale Ergebnisse nicht garantieren. Gebühren, Spreads, Slippage, Liquidität und Ausführung können beim echten Handel abweichen.
