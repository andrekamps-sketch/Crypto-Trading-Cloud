from __future__ import annotations

import hmac
import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st

from cloud_core import (
    CENTRAL_HISTORY, DATA_DIR, MONITOR_LATEST, V52_STATE, V6_STATE,
    evaluate_all, latest_combined, read_json, save_state, scan_market, state_status,
)
from monitor import FROZEN_QUALITY

TITLE = os.environ.get("TRADING_DASHBOARD_TITLE", "Crypto Trading Zentrale – V6.3 Cloud")
PASSWORD = os.environ.get("TRADING_DASHBOARD_PASSWORD", "")

st.set_page_config(page_title=TITLE, page_icon="☁️", layout="wide")

st.markdown("""
<style>
@media (max-width: 760px) {
  .block-container {padding-top: .7rem !important; padding-left: .65rem !important; padding-right: .65rem !important;}
  h1 {font-size: 1.55rem !important; line-height: 1.15 !important;}
  h2 {font-size: 1.3rem !important;}
  h3 {font-size: 1.1rem !important;}
  div[data-testid="stMetric"] {padding: .35rem .25rem !important;}
  div[data-testid="stMetricValue"] {font-size: 1.3rem !important;}
  div[data-testid="column"] {min-width: 100% !important; flex: 1 1 100% !important;}
  .stButton > button, .stDownloadButton > button {width: 100% !important; min-height: 2.7rem;}
}
</style>
""", unsafe_allow_html=True)


def auth_gate() -> None:
    if not PASSWORD:
        st.warning("⚠️ Cloud-Passwort ist noch nicht gesetzt. Vor einer öffentlichen Bereitstellung die Umgebungsvariable `TRADING_DASHBOARD_PASSWORD` setzen.")
        return
    if st.session_state.get("cloud_authenticated"):
        return
    st.title("🔐 Trading-Zentrale")
    st.caption("Privater Zugriff auf die Paper-Trading-Zentrale.")
    pw = st.text_input("Passwort", type="password")
    if st.button("Anmelden", type="primary"):
        if hmac.compare_digest(pw, PASSWORD):
            st.session_state["cloud_authenticated"] = True
            st.rerun()
        else:
            st.error("Passwort nicht korrekt.")
    st.stop()


auth_gate()

st.title("☁️ " + TITLE)
st.caption("Cloud-/Handy-Zentrale für die eingefrorenen Paper-Tests. Keine echten Broker-Orders und keine Live-Trading-Berechtigung.")

status = state_status()
with st.sidebar:
    st.header("Cloud-Status")
    st.write(("✅" if status["v52"] else "⚪") + " V5.2 Forward-Wettkampf")
    st.write(("✅" if status["v6"] else "⚪") + " V6 Experimente")
    worker = status.get("worker") or {}
    if worker:
        st.caption("Worker: " + ("✅ " if worker.get("ok") else "⚠️ ") + str(worker.get("message", "")))
        st.caption("Letzter Lauf: " + str(worker.get("updated_at", "–")))
    else:
        st.caption("Worker hat noch keinen Status gespeichert.")
    st.markdown("---")
    st.caption(f"Persistenter Datenpfad: {DATA_DIR}")
    if PASSWORD and st.button("Abmelden"):
        st.session_state["cloud_authenticated"] = False
        st.rerun()


def show_table(table: pd.DataFrame) -> None:
    if table is None or table.empty:
        st.info("Noch keine auswertbaren Kontowerte gespeichert.")
        return
    st.dataframe(table, use_container_width=True, hide_index=True)
    avail = table[pd.to_numeric(table.get("Kontowert €"), errors="coerce").notna()].copy()
    if not avail.empty:
        avail["Kontowert €"] = pd.to_numeric(avail["Kontowert €"])
        avail = avail.sort_values("Kontowert €", ascending=False)
        leader = avail.iloc[0]
        a, b, c, d = st.columns(4)
        a.metric("Aktuell vorne", str(leader.get("Strategie", "–")))
        b.metric("Kontowert", f"{float(leader['Kontowert €']):,.2f} €")
        rv = pd.to_numeric(pd.Series([leader.get("Rendite %")]), errors="coerce").iloc[0]
        c.metric("Rendite", f"{rv:+.2f} %" if pd.notna(rv) else "–")
        d.metric("Tests mit Wert", str(len(avail)))


tab0, tab1, tab2, tab3, tab4 = st.tabs(["🏠 Zentrale", "📡 Markt-Monitor", "📱 Handy", "⚙️ Cloud-Setup", "📁 Verlauf"])

with tab0:
    st.subheader("Forward-Tests in der Cloud")
    c1, c2 = st.columns(2)
    with c1:
        if status["v52"]:
            ts = pd.Timestamp(status["v52"]["started_at"])
            st.success(f"V5.2 eingefroren seit {ts.strftime('%d.%m.%Y %H:%M')}")
        else:
            st.info("V5.2-State noch nicht importiert.")
    with c2:
        if status["v6"]:
            ts = pd.Timestamp(status["v6"]["start_utc"])
            try:
                ts = ts.tz_convert("Europe/Berlin") if ts.tzinfo else ts
            except Exception:
                pass
            st.success(f"V6 eingefroren seit {ts.strftime('%d.%m.%Y %H:%M')}")
        else:
            st.info("V6-State noch nicht importiert.")

    if st.button("🔄 Jetzt mit neuen Kursdaten auswerten", type="primary"):
        try:
            with st.spinner("V5.2 und V6 werden nachgerechnet …"):
                table = evaluate_all()
            st.session_state["cloud_table"] = table
            st.success("Aktualisiert. Die eingefrorenen Regeln und Startzeitpunkte wurden nicht verändert.")
        except Exception as exc:
            st.error(f"Auswertung fehlgeschlagen: {exc}")

    table = st.session_state.get("cloud_table")
    if not isinstance(table, pd.DataFrame) or table.empty:
        table = latest_combined()
    show_table(table)
    st.caption("Zwischenstände sind keine Gewinnprognose. Besonders bei wenigen Trades können Rangfolge und Rendite stark schwanken.")

with tab1:
    st.subheader("📡 Markt-Monitor")
    st.caption("Beobachtet größere Coins und erklärt, wie nah sie an den eingefrorenen Quality-Breakout-Bedingungen liegen. Der Monitor verändert die Forward-Tests nicht.")
    default_assets = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT", "BCH"]
    selected = st.multiselect("Coins", default_assets, default=default_assets)
    if st.button("📡 Markt jetzt scannen", type="primary"):
        try:
            with st.spinner("60 Tage 1h-Daten werden geladen …"):
                payload = scan_market(selected)
            st.session_state["cloud_monitor"] = payload
        except Exception as exc:
            st.error(f"Markt-Scan fehlgeschlagen: {exc}")
    payload = st.session_state.get("cloud_monitor") or read_json(MONITOR_LATEST, {})
    if payload:
        reg = payload.get("regime")
        if reg:
            st.markdown(f"### Gesamtmarkt: {reg.get('emoji','')} **{reg.get('label','')}**")
            st.caption(str(reg.get("reason", "")))
        rows = payload.get("rows", [])
        if rows:
            near = rows[0]
            ready = [r for r in rows if r.get("quality_ready")]
            a, b, c, d = st.columns(4)
            a.metric("Quality-ready", len(ready))
            b.metric("Nächster Kandidat", near.get("Coin", "–"))
            c.metric("Regeln erfüllt", str(near.get("Quality", "–")))
            d.metric("Quality-Edge", f"{float(near.get('Edge',0)):.1f}")
            if ready:
                st.success("Quality-Bedingungen erfüllt: " + ", ".join(str(r.get("Coin")) for r in ready))
            else:
                st.info(f"Noch kein Quality-Setup. Bei **{near.get('Coin','–')}** fehlt aktuell: {near.get('Fehlt','–')}")
            df = pd.DataFrame(rows)
            display_cols = [c for c in ["Coin", "Quality", "Score", "Edge", "Signal", "Risiko", "Preis €", "RSI", "Volumen x", "24h %", "7T %", "Fehlt"] if c in df.columns]
            st.dataframe(df[display_cols], use_container_width=True, hide_index=True)
            st.download_button("⬇ Markt-Scan CSV", df.to_csv(index=False).encode("utf-8-sig"), "markt_monitor.csv", "text/csv")
            with st.expander("Eingefrorene Quality-Schwellen"):
                st.json(FROZEN_QUALITY)
    else:
        st.info("Noch kein Markt-Scan gespeichert.")

with tab2:
    st.subheader("📱 Handy-Kurzansicht")
    table = latest_combined()
    if not table.empty:
        avail = table[pd.to_numeric(table["Kontowert €"], errors="coerce").notna()].copy()
        avail["Kontowert €"] = pd.to_numeric(avail["Kontowert €"], errors="coerce")
        avail = avail.sort_values("Kontowert €", ascending=False)
        for _, r in avail.iterrows():
            with st.container(border=True):
                a, b = st.columns(2)
                a.markdown(f"**{r.get('Strategie','–')}**")
                a.metric("Kontowert", f"{float(r['Kontowert €']):,.2f} €")
                ret = pd.to_numeric(pd.Series([r.get("Rendite %")]), errors="coerce").iloc[0]
                dd = pd.to_numeric(pd.Series([r.get("Drawdown %")]), errors="coerce").iloc[0]
                b.metric("Rendite", f"{ret:+.2f} %" if pd.notna(ret) else "–")
                b.metric("Drawdown", f"{dd:.2f} %" if pd.notna(dd) else "–")
                st.caption(f"{r.get('System','')} · Trades: {r.get('Trades',0)} · {r.get('Status','')}")
    else:
        st.info("Noch keine Paper-Auswertung gespeichert.")
    payload = read_json(MONITOR_LATEST, {})
    if payload and payload.get("rows"):
        st.markdown("### Top-Markt-Kandidaten")
        for r in payload["rows"][:5]:
            icon = "✅" if r.get("quality_ready") else "🟡"
            st.write(f"{icon} **{r.get('Coin')}** · {r.get('Quality')} Regeln · Edge {float(r.get('Edge',0)):.1f} · RSI {float(r.get('RSI',0)):.1f}")
            if r.get("Fehlt"):
                st.caption("Fehlt: " + str(r.get("Fehlt")))

with tab3:
    st.subheader("⚙️ Cloud-Setup")
    st.write("Hier importierst du nur die zwei kleinen eingefrorenen Zustandsdateien von deinem PC. Die Originaldateien bleiben unverändert.")
    st.markdown("**V5.2:** `forward_competition.json`  ·  **V6:** `v6_forward_state.json`")
    up52 = st.file_uploader("V5.2-State hochladen", type=["json"], key="v52upload")
    if up52 is not None and st.button("V5.2-State speichern"):
        try:
            payload = json.loads(up52.getvalue().decode("utf-8"))
            save_state("v52", payload)
            st.success("V5.2-State gespeichert.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    up6 = st.file_uploader("V6-State hochladen", type=["json"], key="v6upload")
    if up6 is not None and st.button("V6-State speichern"):
        try:
            payload = json.loads(up6.getvalue().decode("utf-8"))
            save_state("v6", payload)
            st.success("V6-State gespeichert.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.markdown("### Sicherheit")
    if PASSWORD:
        st.success("Dashboard-Passwort ist als Umgebungsvariable gesetzt.")
    else:
        st.error("Noch kein `TRADING_DASHBOARD_PASSWORD` gesetzt. So nicht öffentlich ins Internet stellen.")
    st.write("Broker-API-Schlüssel gehören später ausschließlich in Server-Secrets/Umgebungsvariablen, niemals in ZIP-Dateien oder Quellcode. V6.3 enthält aktuell überhaupt keine Order-Funktion.")
    st.markdown("### 24/7-Aktualisierung")
    worker = state_status().get("worker") or {}
    if worker:
        st.json(worker)
    else:
        st.info("Auf einem Cloud-Server startet `worker.py` automatisch mit. Standard: eine Aktualisierung pro Stunde.")

with tab4:
    st.subheader("📁 Verlauf & Export")
    if CENTRAL_HISTORY.exists():
        try:
            hist = pd.read_csv(CENTRAL_HISTORY)
            st.dataframe(hist.tail(200), use_container_width=True, hide_index=True)
            st.download_button("⬇ Gesamten Verlauf herunterladen", hist.to_csv(index=False).encode("utf-8-sig"), "cloud_trading_verlauf.csv", "text/csv")
            val = hist[pd.to_numeric(hist.get("Kontowert €"), errors="coerce").notna()].copy()
            if not val.empty:
                val["Kontowert €"] = pd.to_numeric(val["Kontowert €"])
                piv = val.pivot_table(index="timestamp", columns="Strategie", values="Kontowert €", aggfunc="last")
                if len(piv) > 1:
                    st.line_chart(piv)
        except Exception as exc:
            st.warning(f"Verlauf konnte nicht gelesen werden: {exc}")
    else:
        st.info("Noch kein Verlauf vorhanden. Der Cloud-Worker oder eine manuelle Auswertung legt ihn automatisch an.")

st.markdown("---")
st.caption("V6.3 Cloud ist weiterhin reines Paper-Trading/Monitoring. Es gibt keine automatische echte Order-Ausführung.")
