# Crypto Trading Zentrale – V6.5 Cloud

V6.5 erweitert die bestehende V6.4.1-Cloud-Zentrale nur bei den Benachrichtigungen. Die eingefrorenen V5.2-/V6-Paper-Tests und das Railway-Volume `/data` bleiben unverändert.

## Neu in V6.5

- **Paper-Trade-Meldungen mit mehr Details**
  - Kauf/Verkauf, Coin, Preis, Uhrzeit, Grund und Gebühr
  - bei Verkäufen: realisierter Gewinn/Verlust in Euro
  - aktueller Kontowert der betroffenen Strategie, sofern verfügbar
- **getrennte Quality-Alarme**
  - 🟡 9/10 = Vorwarnung
  - 🚀 10/10 bzw. READY = stärkerer Alarm
  - ein Coin kann später erneut alarmieren, wenn er die Schwelle erst verliert und anschließend wieder erreicht
- **ausführlichere tägliche Telegram-Zusammenfassung**
  - Rangliste/Kontowerte aller Paper-Strategien
  - Veränderung seit der letzten Tagesübersicht
  - Trades und Gebühren seit Start
  - neue Aktionen und realisiertes G/V seit der letzten Übersicht
  - Marktphase und Top-3-Markt-Kandidaten
- Im Dashboard gibt es zwei Komfort-Buttons:
  - **Tagesübersicht jetzt senden**
  - **Aktuelles Top-Signal senden**

## Update der laufenden Railway-App

Am einfachsten nur die Patch-Dateien aus `telegram_comfort_patch_v6_5.zip` in das **Hauptverzeichnis** deines bestehenden GitHub-Repositories `Crypto-Trading-Cloud` hochladen und vorhandene Dateien ersetzen.

Danach `Commit changes`. Railway deployt automatisch neu.

**Nicht löschen oder verändern:**
- Railway-Volume `crypto-trading-cloud-volume`
- Mount Path `/data`
- deine bestehenden Telegram-/Passwort-Variablen

Die vorhandene `notification_state.json` wird kompatibel weiterverwendet. Beim ersten V6.5-Lauf wird der neue 9/10-/10/10-Status als Baseline übernommen, damit nach dem Update keine alten Quality-Signale nachträglich gespammt werden.

## Optionale Variablen

```text
NOTIFY_TRADES=1
NOTIFY_MARKET_CANDIDATES=1
NOTIFY_QUALITY_9=1
NOTIFY_QUALITY_10=1
NOTIFY_LEADER_CHANGE=1
NOTIFY_DAILY_SUMMARY=1
DAILY_SUMMARY_HOUR=20
NOTIFY_TIMEZONE=Europe/Berlin
```

Wenn die beiden neuen Variablen `NOTIFY_QUALITY_9` und `NOTIFY_QUALITY_10` nicht gesetzt werden, sind beide standardmäßig **aktiv**.

V6.5 bleibt reines Paper-Trading/Monitoring und enthält keine echte Order-Ausführung.
