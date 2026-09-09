from __future__ import annotations

import hmac
import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st

from cloud_core import (
    CENTRAL_HISTORY, DATA_DIR, MONITOR_LATEST, V52_STATE, V6_STATE, V52_LATEST, V6_LATEST,
    evaluate_all, latest_combined, read_json, save_state, scan_market, state_status,
)
from monitor import FROZEN_QUALITY
from notifications import (
    clear_saved_chat_id, discover_chat_id, notification_log, build_daily_summary, process_notifications, send_telegram, settings as notification_settings, save_chat_id, validate_chat_id,
)
from signal_lab import coin_stats, events_dataframe, horizon_stats, lab_status, process_signal_lab
from strategy_challenger import (
    CANDIDATES, PROMOTION_RULES, challenge_history, challenge_state, challenge_table,
    process_challenge, start_challenge,
)
from live_paper import (
    DEFAULT_ASSETS as LIVE_DEFAULT_ASSETS, DEFAULTS as LIVE_DEFAULTS, LIVE_HISTORY, LIVE_TRADES,
    history_dataframe as live_history_dataframe, market_dataframe as live_market_dataframe,
    positions_dataframe as live_positions_dataframe, process_live_paper, resume_live_paper,
    set_entry_paused, start_live_paper, state as live_paper_state, stop_live_paper, trades_dataframe as live_trades_dataframe,
)
from live_paper_long_short import (
    DEFAULT_ASSETS as LS_DEFAULT_ASSETS, DEFAULTS as LS_DEFAULTS,
    history_dataframe as ls_history_dataframe, market_dataframe as ls_market_dataframe,
    positions_dataframe as ls_positions_dataframe, process_long_short_paper, resume_long_short_paper,
    set_entry_paused as set_ls_entry_paused, start_long_short_paper, state as long_short_state,
    stop_long_short_paper, trades_dataframe as ls_trades_dataframe,
)
from fee_aware_grid import (
    DEFAULTS as FEE_GRID_DEFAULTS, history_dataframe as fee_grid_history, latest as fee_grid_latest,
    process_fee_grid, result_dataframe as fee_grid_results, resume_fee_grid,
    start_fee_grid, state as fee_grid_state, stop_fee_grid,
)

TITLE = os.environ.get("TRADING_DASHBOARD_TITLE", "Crypto Trading Zentrale – V6.10 Cloud")
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
    ch_side = challenge_state()
    st.write(("✅" if ch_side and ch_side.get("active") else "⚪") + " Strategy Challenger")
    lp_side = live_paper_state()
    st.write(("✅" if lp_side and lp_side.get("active") else "⚪") + " Multi-Coin Live-Paper Long-only")
    ls_side = long_short_state()
    st.write(("✅" if ls_side and ls_side.get("active") else "⚪") + " Live-Paper Long+Short")
    fg_side = fee_grid_state()
    st.write(("✅" if fg_side and fg_side.get("active") else "⚪") + " Fee-Aware Grid Vergleich")
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


tab0, tab1, tab_lab, tab_challenge, tab_live, tab_ls, tab_feegrid, tab2, tab3, tab4, tab5 = st.tabs(["🏠 Zentrale", "📡 Markt-Monitor", "🧪 Signal-Labor", "🏆 Strategy Challenger", "🎮 Live-Paper", "↕️ Long+Short", "💸 Fee-Grid", "📱 Handy", "🔔 Benachrichtigungen", "⚙️ Cloud-Setup", "📁 Verlauf"])

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
            process_notifications(
                table, read_json(MONITOR_LATEST, {}) or {},
                read_json(V52_LATEST, {}) or {}, read_json(V6_LATEST, {}) or {},
            )
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
            lab_result = process_signal_lab(payload)
            if lab_result.get("ok") and lab_result.get("new_signals", 0):
                st.toast(f"Signal-Labor: {lab_result.get('new_signals', 0)} neues Signal gespeichert")
            process_notifications(
                latest_combined(), payload,
                read_json(V52_LATEST, {}) or {}, read_json(V6_LATEST, {}) or {},
            )
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

with tab_lab:
    st.subheader("🧪 Quality-Signal-Labor")
    st.caption("V6.6 protokolliert 9/10- und 10/10-Quality-Signale und misst anschließend die Kursentwicklung. Das Labor beobachtet nur und verändert weder V5.2/V6-Regeln noch Paper-Positionen.")

    mon_now = read_json(MONITOR_LATEST, {}) or {}
    if st.button("🧪 Aktuellen Markt-Scan ins Signal-Labor übernehmen", type="primary", use_container_width=True):
        if not mon_now.get("rows"):
            st.warning("Noch kein Markt-Scan vorhanden. Erst im Markt-Monitor scannen.")
        else:
            res = process_signal_lab(mon_now)
            if res.get("ok"):
                st.success(f"Signal-Labor aktualisiert: {res.get('total', 0)} Signale · {res.get('new_signals', 0)} neu · {res.get('checkpoints', 0)} neue Zeit-Checkpoints.")
            else:
                st.warning(str(res.get("message", "Signal-Labor konnte nicht aktualisiert werden.")))

    lab = lab_status()
    a, b, c, d = st.columns(4)
    a.metric("Signale gesamt", lab.get("total", 0))
    b.metric("9/10", lab.get("signals_9", 0))
    c.metric("10/10", lab.get("signals_10", 0))
    d.metric("24h auswertbar", lab.get("complete_24h", 0))
    if lab.get("last_scan_at"):
        st.caption("Letzte Signal-Labor-Aktualisierung: " + str(lab.get("last_scan_at")))
    else:
        st.info("Das Signal-Labor startet beim nächsten automatischen Markt-Scan. Bereits aktive 9/10- oder 10/10-Kandidaten werden einmalig als **Baseline** gespeichert.")

    ev = events_dataframe()
    if not ev.empty:
        st.markdown("### Letzte Signale")
        show = ev.copy()
        rename = {
            "signal_time": "Zeit", "asset": "Coin", "stage": "Stage", "trigger": "Typ",
            "signal_price": "Signalpreis €", "edge": "Edge", "rsi": "RSI", "market_regime": "Markt",
            "missing": "Fehlte", "ret_1h_pct": "1h %", "ret_6h_pct": "6h %", "ret_24h_pct": "24h %",
            "ret_3d_pct": "3T %", "ret_7d_pct": "7T %", "max_up_pct": "Max +%", "max_down_pct": "Max -%",
        }
        cols = [c for c in rename if c in show.columns]
        show = show[cols].rename(columns=rename)
        if "Zeit" in show.columns:
            try:
                show["Zeit"] = pd.to_datetime(show["Zeit"], utc=True).dt.tz_convert("Europe/Berlin").dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass
        st.dataframe(show.head(200), use_container_width=True, hide_index=True)
        st.download_button("⬇ Signal-Labor CSV", ev.to_csv(index=False).encode("utf-8-sig"), "quality_signal_labor.csv", "text/csv")

        include_base = st.checkbox("V6.6-Start-Baseline in Statistik einbeziehen", value=True, help="Baseline-Signale waren beim Start von V6.6 bereits aktiv. Sie sind nützlich für die Kursbeobachtung, aber keine neu ausgelösten Übergänge.")
        hs = horizon_stats(include_baseline=include_base)
        st.markdown("### Ergebnis nach Zeitabstand")
        if hs.empty:
            st.info("Noch keine Zeit-Checkpoints abgeschlossen. Nach 1 Stunde erscheint die erste Auswertung; 6h/24h/3T/7T folgen automatisch.")
        else:
            st.dataframe(hs, use_container_width=True, hide_index=True)

        cs = coin_stats("24h", include_baseline=include_base)
        st.markdown("### Coins nach 24 Stunden")
        if cs.empty:
            st.info("Noch keine 24h-Signale auswertbar.")
        else:
            st.dataframe(cs, use_container_width=True, hide_index=True)

        st.markdown("### So lesen wir die Werte")
        st.caption("Positiv % = Anteil der Signale, deren Kurs am jeweiligen Checkpoint über dem Signalpreis lag. Max +% / Max -% zeigen die bislang beobachtete beste bzw. schlechteste Bewegung seit dem Signal. Erst mit genügend Signalen wird daraus eine belastbare Statistik.")
    else:
        st.info("Noch keine 9/10- oder 10/10-Signale gespeichert.")


with tab_challenge:
    st.subheader("🏆 Strategy Challenger")
    st.caption("Neue Regeln laufen ausschließlich im Shadow-/Paper-Modus gegen einen eingefrorenen Champion. Eine Promotion wird nur empfohlen – niemals automatisch ausgeführt.")

    ch_state = challenge_state()
    live_table = latest_combined()
    if not ch_state:
        st.info("Noch keine Challenger-Saison gestartet. Beim Start werden Champion, Universum, Gebühren und Promotion-Regeln eingefroren. Bereits vorhandene Kurse vor dem Start werden nicht als Challenger-Trades verwendet.")
        if live_table.empty:
            st.warning("Noch keine Paper-Kontowerte vorhanden. Erst die Zentrale auswerten lassen.")
        else:
            available = live_table[pd.to_numeric(live_table.get("Kontowert €"), errors="coerce").notna()].copy()
            champions = available["Strategie"].astype(str).tolist() if not available.empty else []
            default_idx = champions.index("Quality Breakout") if "Quality Breakout" in champions else 0
            champion_choice = st.selectbox("Champion", champions, index=default_idx if champions else 0, disabled=not champions)
            v52_state_now = state_status().get("v52") or {}
            frozen_assets = list(v52_state_now.get("assets") or ["BTC", "ETH", "SOL", "LINK", "AVAX"])
            fee_pct = float(v52_state_now.get("fee_pct", 0.25) or 0.25)
            st.write("**Eingefrorenes Coin-Universum:** " + ", ".join(frozen_assets))
            st.write(f"**Simulierte Gebühr:** {fee_pct:.2f}%")
            st.markdown("### Neue Shadow-Challenger")
            for name, rules in CANDIDATES.items():
                with st.container(border=True):
                    st.markdown(f"**{name}**")
                    st.caption(str(rules.get("description", "")))
                    st.write(f"Entry: ≥{rules['min_rules']}/10 · Edge ≥{rules['min_edge']:.0f} · Bestätigungen {rules['confirm_scans']}")
                    st.write(f"Risiko: 20% Allokation · SL {rules['stop_loss_pct']:.0f}% · TP {rules['take_profit_pct']:.0f}% · Trailing {rules['trailing_stop_pct']:.0f}% ab +{rules['trailing_activation_pct']:.0f}%")
            with st.expander("Promotion-Regeln"):
                st.write(f"Mindestens {PROMOTION_RULES['min_days']:.0f} Tage Forward-Laufzeit")
                st.write(f"Mindestens {PROMOTION_RULES['min_completed_trades']} abgeschlossene Trades")
                st.write(f"Mindestens +{PROMOTION_RULES['min_outperformance_pp']:.1f} Prozentpunkte vor dem Champion")
                st.write(f"Profit Factor ≥ {PROMOTION_RULES['min_profit_factor']:.2f}")
                st.write(f"Max. Drawdown ≤ {PROMOTION_RULES['max_drawdown_pct']:.1f}%")
                st.write(f"Alle Kriterien müssen anschließend {PROMOTION_RULES['qualification_hold_hours']:.0f} Stunden stabil bleiben")
            if champions and st.button("🏁 Challenger-Saison ab jetzt einfrieren", type="primary", use_container_width=True):
                try:
                    start_challenge(live_table, champion_choice, frozen_assets, 1000.0, fee_pct)
                    st.success("Challenge gestartet. Die ersten Shadow-Trades dürfen erst mit einem neuen Markt-Scan nach diesem Zeitpunkt entstehen.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
    else:
        started = pd.Timestamp(ch_state.get("started_at"))
        try:
            started_local = started.tz_convert("Europe/Berlin") if started.tzinfo else started
        except Exception:
            started_local = started
        elapsed_days = max(0.0, (pd.Timestamp.now(tz="UTC") - (started.tz_convert("UTC") if started.tzinfo else started.tz_localize("UTC"))).total_seconds() / 86400.0)
        a, b, c, d = st.columns(4)
        a.metric("Champion", str(ch_state.get("champion", "–")))
        b.metric("Laufzeit", f"{elapsed_days:.1f} Tage")
        c.metric("Challenger", str(len(ch_state.get("challengers", {}))))
        d.metric("Regeln", "🔒 eingefroren")
        st.caption("Start: " + started_local.strftime("%d.%m.%Y %H:%M") + " · Universum: " + ", ".join(ch_state.get("assets", [])))

        if st.button("🔄 Challenger mit letztem Scan aktualisieren", use_container_width=True):
            try:
                res = process_challenge(latest_combined(), read_json(MONITOR_LATEST, {}) or {})
                st.success(str(res.get("message", "Aktualisiert")))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        ct = challenge_table(ch_state)
        if not ct.empty:
            st.dataframe(ct, use_container_width=True, hide_index=True)

        last_metrics = ch_state.get("last_metrics") or {}
        champ = last_metrics.get("champion") or {}
        st.markdown("### Promotion-Prüfung")
        for name, shadow in (ch_state.get("challengers") or {}).items():
            m = (last_metrics.get("challengers") or {}).get(name) or {}
            with st.container(border=True):
                st.markdown(f"**{name}** · {m.get('status', shadow.get('last_status','🟡 BEOBACHTEN'))}")
                x1, x2, x3, x4 = st.columns(4)
                x1.metric("Netto seit Start", f"{float(m.get('return_pct',0)):+.2f}%")
                x2.metric("Champion", f"{float(champ.get('return_pct',0)):+.2f}%")
                x3.metric("Drawdown", f"{float(m.get('drawdown_pct',0)):.2f}%")
                pfv = m.get("profit_factor", 0)
                x4.metric("Profit Factor", "∞" if pfv == float('inf') else f"{float(pfv):.2f}")
                for check in m.get("checks", []):
                    st.caption(str(check))
                pos = shadow.get("position") or {}
                if pos:
                    st.write(f"Offene Shadow-Position: **{pos.get('asset')}** · Einstieg {float(pos.get('entry_price',0)):,.4f} €")
                else:
                    st.write("Position: **Cash**")

        hist_ch = challenge_history()
        if not hist_ch.empty:
            st.markdown("### Forward-Verlauf seit Challenge-Start")
            try:
                hv = hist_ch.copy()
                hv["Kontowert €"] = pd.to_numeric(hv["Kontowert €"], errors="coerce")
                piv = hv.pivot_table(index="timestamp", columns="Strategie", values="Kontowert €", aggfunc="last")
                if len(piv) > 1:
                    st.line_chart(piv)
            except Exception:
                pass
            st.download_button("⬇ Challenger-Verlauf CSV", hist_ch.to_csv(index=False).encode("utf-8-sig"), "strategy_challenger_verlauf.csv", "text/csv")

        st.warning("Eine Promotion ändert keine laufende Strategie automatisch. Selbst bei 'PROMOTION EMPFOHLEN' bleibt der Champion unverändert, bis du ausdrücklich entscheidest.")


with tab_live:
    st.subheader("🎮 Multi-Coin Live-Paper")
    st.caption("Aktuelle reale Marktpreise, aber ausschließlich Spielgeld. V6.8 enthält weiterhin keinerlei echte Broker-/Bitpanda-Orderfunktion.")
    lp = live_paper_state()
    monitor_now = read_json(MONITOR_LATEST, {}) or {}

    if not lp:
        st.info("Noch kein Live-Paper-Konto gestartet. Beim Start werden die Regeln und das Spielgeld separat von V5.2/V6/Challenger gespeichert.")
        assets_lp = st.multiselect("Coins für Live-Paper", LIVE_DEFAULT_ASSETS, default=LIVE_DEFAULT_ASSETS, key="live_assets_start")
        c1, c2, c3 = st.columns(3)
        start_cap = c1.number_input("Spielgeld €", min_value=100.0, max_value=100000.0, value=1000.0, step=100.0)
        position_pct = c2.number_input("Position je Einstieg %", min_value=5.0, max_value=50.0, value=20.0, step=5.0)
        max_positions = c3.number_input("Max. offene Positionen", min_value=1, max_value=6, value=3, step=1)
        c4, c5, c6 = st.columns(3)
        max_invested = c4.number_input("Max. investiert %", min_value=10.0, max_value=100.0, value=60.0, step=10.0)
        fee_pct = c5.number_input("Simulierte Gebühr %", min_value=0.0, max_value=2.0, value=0.25, step=0.05, format="%.2f")
        slippage_pct = c6.number_input("Simulierte Slippage %", min_value=0.0, max_value=1.0, value=0.10, step=0.05, format="%.2f")
        c7, c8, c9 = st.columns(3)
        min_rules = c7.selectbox("Mindestens Quality-Regeln", [8, 9, 10], index=1)
        min_edge = c8.number_input("Mindest-Edge", min_value=70.0, max_value=100.0, value=85.0, step=1.0)
        confirmations = c9.selectbox("Bestätigungs-Scans", [1, 2, 3], index=0)
        with st.expander("Eingefrorenes Risiko-Setup"):
            st.write("Stop-Loss **4%** · Take-Profit **8%** · Trailing **3%** ab **+4%** · Cooldown nach Exit **6h**")
            st.write("Der Worker aktualisiert offene Positionen standardmäßig alle **5 Minuten** mit dem neuesten verfügbaren 5-Minuten-Kurs. Qualitätssignale stammen aus dem stündlichen Markt-Scan.")
        if st.button("🎮 Live-Paper mit Spielgeld starten", type="primary", use_container_width=True):
            try:
                start_live_paper(
                    assets_lp, start_capital=float(start_cap), position_pct=float(position_pct),
                    max_positions=int(max_positions), max_invested_pct=float(max_invested),
                    fee_pct=float(fee_pct), slippage_pct=float(slippage_pct),
                    min_rules=int(min_rules), min_edge=float(min_edge), confirm_scans=int(confirmations),
                )
                st.success("Multi-Coin Live-Paper gestartet. Es können niemals echte Orders gesendet werden.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    else:
        cfg_lp = lp.get("params") or {}
        start_cap = float(cfg_lp.get("start_capital", 1000.0))
        equity = float(lp.get("equity", start_cap))
        cash = float(lp.get("cash", start_cap))
        open_count = len(lp.get("positions", {}) or {})
        ret = (equity / start_cap - 1) * 100 if start_cap else 0.0
        x1, x2, x3, x4 = st.columns(4)
        x1.metric("Live-Paper Konto", f"{equity:,.2f} €", f"{ret:+.2f}%")
        x2.metric("Freies Spielgeld", f"{cash:,.2f} €")
        x3.metric("Offene Positionen", f"{open_count}/{int(cfg_lp.get('max_positions',3))}")
        x4.metric("Max. Drawdown", f"{float(lp.get('max_drawdown_pct',0)):.2f}%")
        y1, y2, y3, y4 = st.columns(4)
        y1.metric("Realisierter G/V", f"{float(lp.get('realized_pnl',0)):+.2f} €")
        y2.metric("Gebühren", f"{float(lp.get('fees_total',0)):.2f} €")
        y3.metric("Geschlossene Trades", str(int(lp.get('closed_trades',0))))
        wins = int(lp.get('wins',0)); losses = int(lp.get('losses',0))
        hit = wins/(wins+losses)*100 if wins+losses else 0.0
        y4.metric("Trefferquote", f"{hit:.1f}%" if wins+losses else "–")

        started = pd.Timestamp(lp.get("started_at"))
        try:
            started = started.tz_convert("Europe/Berlin") if started.tzinfo else started
        except Exception:
            pass
        st.caption("Start: " + started.strftime("%d.%m.%Y %H:%M") + " · Coins: " + ", ".join(lp.get("assets", [])))
        st.caption(f"Signal: ≥{int(cfg_lp.get('min_rules',9))}/10 · Edge ≥{float(cfg_lp.get('min_edge',85)):.0f} · Position {float(cfg_lp.get('position_pct',20)):.0f}% · max. investiert {float(cfg_lp.get('max_invested_pct',60)):.0f}%")

        c1, c2, c3 = st.columns(3)
        if c1.button("⚡ Jetzt mit aktuellen Kursen prüfen", type="primary", use_container_width=True):
            try:
                res = process_live_paper(read_json(MONITOR_LATEST, {}) or {})
                for event in res.get("events", []) or []:
                    txt = str(event.get("telegram", "")).strip()
                    if txt:
                        send_telegram(txt)
                st.success(str(res.get("message", "Live-Paper aktualisiert")))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        if lp.get("entry_paused"):
            if c2.button("▶ Neue Einstiege fortsetzen", use_container_width=True):
                set_entry_paused(False); st.rerun()
        else:
            if c2.button("⏸ Neue Einstiege pausieren", use_container_width=True):
                set_entry_paused(True); st.rerun()
        if lp.get("active", True):
            if c3.button("⏹ Live-Paper stoppen", use_container_width=True):
                stop_live_paper(); st.rerun()
        else:
            if c3.button("▶ Live-Paper fortsetzen", use_container_width=True):
                resume_live_paper(); st.rerun()

        if lp.get("entry_paused"):
            st.warning("Neue Einstiege sind pausiert. Bereits offene Positionen werden beim aktiven Worker weiter über Stop/Take/Trailing verwaltet.")
        if not lp.get("active", True):
            st.warning("Live-Paper ist vollständig gestoppt. In diesem Zustand werden auch offene Spielgeld-Positionen nicht automatisch verwaltet.")

        st.markdown("### Offene Spielgeld-Positionen")
        pdf = live_positions_dataframe(lp)
        if pdf.empty:
            st.info("Aktuell keine Position offen. Der Bot wartet auf die stärksten passenden Coins.")
        else:
            st.dataframe(pdf, use_container_width=True, hide_index=True)

        st.markdown("### Aktuelle Coins")
        mdf = live_market_dataframe(monitor_now, lp)
        if not mdf.empty:
            st.dataframe(mdf, use_container_width=True, hide_index=True)
        last_up = lp.get("last_update_at")
        if last_up:
            st.caption("Letzte 5-Minuten-Paper-Prüfung: " + str(last_up) + " · Kursquelle: Yahoo Finance 5m (Monitoring/Paper, nicht exchange-grade Orderbuch).")
        else:
            st.caption("Noch keine 5-Minuten-Paper-Prüfung gespeichert. Der Cloud-Worker oder der Aktualisieren-Button startet sie.")

        hdf = live_history_dataframe(1500)
        if not hdf.empty:
            st.markdown("### Live-Paper Kontoverlauf")
            try:
                chart = hdf.copy()
                chart["timestamp"] = pd.to_datetime(chart["timestamp"], utc=True)
                chart = chart.set_index("timestamp")
                st.line_chart(chart[["equity_eur"]])
            except Exception:
                pass
        tdf = live_trades_dataframe(300)
        if not tdf.empty:
            st.markdown("### Live-Paper Trades")
            st.dataframe(tdf.sort_values("timestamp", ascending=False), use_container_width=True, hide_index=True)
            st.download_button("⬇ Live-Paper Trades CSV", tdf.to_csv(index=False).encode("utf-8-sig"), "live_paper_trades.csv", "text/csv")

        st.info("🎮 **Wichtig:** Das ist echtes Marktgeschehen mit virtuellem Geld. Es gibt in V6.8 keinen Broker-Login, keinen Trading-API-Key und keinen Codepfad zum Senden echter Orders.")


with tab_ls:
    st.subheader("↕️ Multi-Coin Live-Paper Long + Short")
    st.caption("Separater Long+Short-Challenger mit echtem Marktgeschehen und Spielgeld. LONG profitiert von steigenden, SHORT von fallenden Kursen. Keine echten Orders, kein Hebel.")
    ls = long_short_state()
    monitor_ls = read_json(MONITOR_LATEST, {}) or {}

    if not ls:
        st.info("Dieser Test startet mit einem eigenen Spielgeldkonto und verändert das bestehende V6.8 Long-only-Live-Paper nicht.")
        assets_ls = st.multiselect("Coins für Long+Short", LS_DEFAULT_ASSETS, default=LS_DEFAULT_ASSETS, key="ls_assets_start")
        c1, c2, c3 = st.columns(3)
        ls_cap = c1.number_input("Spielgeld Long+Short €", min_value=100.0, max_value=100000.0, value=1000.0, step=100.0, key="ls_cap")
        ls_pos = c2.number_input("Position je Einstieg %", min_value=5.0, max_value=40.0, value=20.0, step=5.0, key="ls_pos")
        ls_max = c3.number_input("Max. offene Positionen", min_value=1, max_value=6, value=3, step=1, key="ls_max")
        c4, c5, c6 = st.columns(3)
        ls_exposure = c4.number_input("Max. Gesamt-Exposure %", min_value=10.0, max_value=100.0, value=60.0, step=10.0, key="ls_exp")
        ls_fee = c5.number_input("Simulierte Gebühr %", min_value=0.0, max_value=2.0, value=0.25, step=0.05, format="%.2f", key="ls_fee")
        ls_slip = c6.number_input("Simulierte Slippage %", min_value=0.0, max_value=1.0, value=0.10, step=0.05, format="%.2f", key="ls_slip")
        c7, c8, c9 = st.columns(3)
        ls_long_rules = c7.selectbox("LONG: Quality-Regeln", [8, 9, 10], index=1, key="ls_long_rules")
        ls_long_edge = c8.number_input("LONG: Mindest-Edge", min_value=70.0, max_value=100.0, value=85.0, step=1.0, key="ls_long_edge")
        ls_conf = c9.selectbox("Bestätigungs-Scans", [1, 2, 3], index=0, key="ls_conf")
        c10, c11, c12 = st.columns(3)
        ls_short_rules = c10.selectbox("SHORT: bearish Regeln", [6, 7, 8], index=1, key="ls_short_rules")
        ls_short_edge = c11.number_input("SHORT: Mindest-Edge", min_value=60.0, max_value=95.0, value=72.0, step=1.0, key="ls_short_edge")
        ls_funding = c12.number_input("SHORT Funding / 8h %", min_value=0.0, max_value=0.20, value=0.01, step=0.01, format="%.2f", key="ls_funding")
        with st.expander("So wird SHORT simuliert"):
            st.write("SHORT ist hier **nur Paper-Trading**. Pro Short wird 1:1 Spielgeld als Sicherheit reserviert – **kein Hebel**.")
            st.write("Ein Short gewinnt, wenn der Coin nach dem virtuellen Einstieg fällt. Stop **+4%**, Ziel **−8%**, Trailing **3%** ab **+4% Gewinn**.")
            st.write("Zusätzlich werden Gebühren, Slippage und ein konservativer Funding-Kostensatz simuliert. Reales Shorting würde später Margin/Derivate erfordern und ist nicht dasselbe wie ein normaler Spot-Kauf bei Bitpanda.")
        if st.button("↕️ Long+Short mit 1.000 € Spielgeld starten", type="primary", use_container_width=True):
            try:
                start_long_short_paper(
                    assets_ls, start_capital=float(ls_cap), position_pct=float(ls_pos), max_positions=int(ls_max),
                    max_exposure_pct=float(ls_exposure), fee_pct=float(ls_fee), slippage_pct=float(ls_slip),
                    long_min_rules=int(ls_long_rules), long_min_edge=float(ls_long_edge), confirm_scans=int(ls_conf),
                    short_min_rules=int(ls_short_rules), short_min_edge=float(ls_short_edge), short_funding_pct_per_8h=float(ls_funding),
                )
                st.success("Long+Short Live-Paper gestartet. Es wird ausschließlich Spielgeld verwendet.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    else:
        cfg_ls = ls.get("params") or {}
        start_ls = float(cfg_ls.get("start_capital", 1000.0)); equity_ls = float(ls.get("equity", start_ls)); cash_ls = float(ls.get("cash", start_ls))
        ret_ls = (equity_ls/start_ls-1)*100 if start_ls else 0.0
        positions_ls = ls.get("positions", {}) or {}
        long_open = sum(1 for p in positions_ls.values() if p.get("side") == "LONG")
        short_open = sum(1 for p in positions_ls.values() if p.get("side") == "SHORT")
        a,b,c,d = st.columns(4)
        a.metric("Long+Short Konto", f"{equity_ls:,.2f} €", f"{ret_ls:+.2f}%")
        b.metric("Freies Spielgeld", f"{cash_ls:,.2f} €")
        c.metric("Offen", f"{len(positions_ls)}/{int(cfg_ls.get('max_positions',3))}", f"LONG {long_open} · SHORT {short_open}")
        d.metric("Max. Drawdown", f"{float(ls.get('max_drawdown_pct',0)):.2f}%")
        e,f,g,h = st.columns(4)
        e.metric("Realisierter G/V", f"{float(ls.get('realized_pnl',0)):+.2f} €")
        f.metric("Gebühren", f"{float(ls.get('fees_total',0)):.2f} €")
        g.metric("Funding bezahlt", f"{float(ls.get('funding_total',0)):.2f} €")
        wins_ls=int(ls.get('wins',0)); losses_ls=int(ls.get('losses',0)); hit_ls=wins_ls/(wins_ls+losses_ls)*100 if wins_ls+losses_ls else 0.0
        h.metric("Trefferquote", f"{hit_ls:.1f}%" if wins_ls+losses_ls else "–")
        st.caption(f"LONG-Einstiege {int(ls.get('long_entries',0))} · SHORT-Einstiege {int(ls.get('short_entries',0))} · geschlossen {int(ls.get('closed_trades',0))}")
        st.caption(f"LONG: ≥{int(cfg_ls.get('long_min_rules',9))}/10 & Edge ≥{float(cfg_ls.get('long_min_edge',85)):.0f} · SHORT: ≥{int(cfg_ls.get('short_min_rules',7))}/8 & Edge ≥{float(cfg_ls.get('short_min_edge',72)):.0f}")

        c1,c2,c3 = st.columns(3)
        if c1.button("⚡ Long+Short jetzt prüfen", type="primary", use_container_width=True):
            try:
                res_ls = process_long_short_paper(read_json(MONITOR_LATEST, {}) or {})
                for event in res_ls.get("events", []) or []:
                    txt = str(event.get("telegram", "")).strip()
                    if txt: send_telegram(txt)
                st.success(str(res_ls.get("message", "aktualisiert"))); st.rerun()
            except Exception as exc:
                st.error(str(exc))
        if ls.get("entry_paused"):
            if c2.button("▶ Neue LONG/SHORT-Einstiege fortsetzen", use_container_width=True): set_ls_entry_paused(False); st.rerun()
        else:
            if c2.button("⏸ Neue Einstiege pausieren", key="ls_pause_new_entries", use_container_width=True): set_ls_entry_paused(True); st.rerun()
        if ls.get("active", True):
            if c3.button("⏹ Long+Short stoppen", use_container_width=True): stop_long_short_paper(); st.rerun()
        else:
            if c3.button("▶ Long+Short fortsetzen", use_container_width=True): resume_long_short_paper(); st.rerun()

        st.markdown("### Offene LONG-/SHORT-Positionen")
        posdf_ls = ls_positions_dataframe(ls)
        if posdf_ls.empty: st.info("Aktuell keine Position. Der Bot wartet auf einen LONG- oder SHORT-Kandidaten.")
        else: st.dataframe(posdf_ls, use_container_width=True, hide_index=True)

        st.markdown("### Multi-Coin Entscheidung")
        market_ls = ls_market_dataframe(monitor_ls, ls)
        if not market_ls.empty: st.dataframe(market_ls, use_container_width=True, hide_index=True)
        st.caption("BULL: neue SHORTs werden gebremst · BEAR: neue LONGs werden gebremst · NEUTRAL: beide Richtungen können antreten.")

        hist_ls = ls_history_dataframe(1500)
        if not hist_ls.empty:
            st.markdown("### Long+Short Kontoverlauf")
            try:
                chart_ls=hist_ls.copy(); chart_ls["timestamp"]=pd.to_datetime(chart_ls["timestamp"], utc=True); chart_ls=chart_ls.set_index("timestamp")
                st.line_chart(chart_ls[["equity_eur"]])
            except Exception: pass
        trades_ls = ls_trades_dataframe(400)
        if not trades_ls.empty:
            st.markdown("### Long+Short Trades")
            st.dataframe(trades_ls.sort_values("timestamp", ascending=False), use_container_width=True, hide_index=True)
            st.download_button("⬇ Long+Short Trades CSV", trades_ls.to_csv(index=False).encode("utf-8-sig"), "live_paper_long_short_trades.csv", "text/csv")

        st.warning("↕️ **Nur Simulation:** SHORTs sind 1x-paper-simulierte Positionen. V6.10 enthält weiterhin keinen Broker-Login, keinen API-Key und keine Funktion für echte Orders.")


with tab_feegrid:
    st.subheader("💸 Fee-Aware Grid Challenger")
    st.caption("Separater Shadow-Vergleich: Das normale BTC-/ETH-Adaptive-Grid läuft ab demselben Startpunkt gegen eine gebührenbewusstere Variante. Die laufenden V6-Grids werden nicht verändert.")
    fg = fee_grid_state()
    if not fg:
        v6s = state_status().get("v6") or {}
        fee_default = float(v6s.get("fee_pct", FEE_GRID_DEFAULTS["fee_pct"]) or FEE_GRID_DEFAULTS["fee_pct"])
        st.info("Noch keine Fee-Grid-Saison gestartet. Beide Varianten beginnen beim Start gleichzeitig mit je 1.000 € Spielgeld pro Coin.")
        a,b,c = st.columns(3)
        capital_fg = a.number_input("Startkapital je Variante (€)", min_value=100.0, value=1000.0, step=100.0, key="fg_capital")
        slip_fg = b.number_input("angenommene Slippage je Order (%)", min_value=0.0, max_value=2.0, value=float(FEE_GRID_DEFAULTS["slippage_pct"]), step=0.05, key="fg_slip")
        buffer_fg = c.number_input("Sicherheitspuffer (%)", min_value=0.0, max_value=3.0, value=float(FEE_GRID_DEFAULTS["safety_buffer_pct"]), step=0.05, key="fg_buffer")
        hurdle = 2*fee_default + 2*slip_fg + buffer_fg
        st.write(f"**Gebühr:** {fee_default:.2f}% je Order · **Kostenhürde für neue Exposition:** ca. {hurdle:.2f}% erwartete Rücklauf-Strecke")
        st.caption("Zusätzlich: 2 abgeschlossene Stunden Bestätigung, mindestens 3 Stunden zwischen Rebalances und mindestens 0,70% Kursweg seit dem letzten Rebalance. Risk-off-Ausstiege werden nie verzögert.")
        if st.button("🏁 Fee-Aware-vs-Standard ab jetzt starten", type="primary", use_container_width=True, key="fg_start"):
            try:
                start_fee_grid(float(capital_fg), fee_default, slippage_pct=float(slip_fg), safety_buffer_pct=float(buffer_fg))
                res = process_fee_grid()
                st.success("Fee-Grid-Vergleich eingefroren. Es zählen nur abgeschlossene 1h-Kerzen ab jetzt.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    else:
        started_fg = pd.Timestamp(fg.get("started_at"))
        try:
            local_fg = started_fg.tz_convert("Europe/Berlin") if started_fg.tzinfo else started_fg
        except Exception:
            local_fg = started_fg
        cfg_fg = fg.get("params") or {}
        latest_fg = fee_grid_latest()
        summary_fg = latest_fg.get("summary", {}) if latest_fg else {}
        a,b,c,d = st.columns(4)
        a.metric("Start", local_fg.strftime("%d.%m. %H:%M"))
        b.metric("Gebühr", f"{float(cfg_fg.get('fee_pct',0.25)):.2f}%")
        hurdle_fg = 2*float(cfg_fg.get('fee_pct',0.25)) + 2*float(cfg_fg.get('slippage_pct',0.10)) + float(cfg_fg.get('safety_buffer_pct',0.35))
        c.metric("Kostenhürde", f"{hurdle_fg:.2f}%")
        d.metric("Status", "läuft" if fg.get("active", True) else "gestoppt")

        cc1,cc2 = st.columns(2)
        if cc1.button("🔄 Fee-Grid jetzt auswerten", type="primary", use_container_width=True, key="fg_update"):
            try:
                res = process_fee_grid()
                st.success(str(res.get("message", "aktualisiert")))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        if fg.get("active", True):
            if cc2.button("⏹ Fee-Grid-Vergleich stoppen", use_container_width=True, key="fg_stop"):
                stop_fee_grid(); st.rerun()
        else:
            if cc2.button("▶ Fee-Grid-Vergleich fortsetzen", use_container_width=True, key="fg_resume"):
                resume_fee_grid(); st.rerun()

        rdf = fee_grid_results()
        if rdf.empty:
            st.info("Noch keine abgeschlossene 1h-Kerze seit dem Start auswertbar.")
        else:
            st.markdown("### Standard gegen Fee-Aware")
            st.dataframe(rdf, use_container_width=True, hide_index=True)
            for asset in ["BTC", "ETH"]:
                x = summary_fg.get(asset, {}) or {}
                advantage = float(x.get("net_advantage_eur", 0) or 0)
                saved = float(x.get("fees_saved_eur", 0) or 0)
                if advantage > 0:
                    st.success(f"{asset}: Fee-Aware liegt aktuell {advantage:+.2f} € vor Standard und hat {saved:+.2f} € Gebühren eingespart.")
                else:
                    st.info(f"{asset}: Fee-Aware liegt aktuell {advantage:+.2f} € gegen Standard; Gebührenersparnis {saved:+.2f} €.")

        hfg = fee_grid_history(3000)
        if not hfg.empty:
            st.markdown("### Kontoverlauf ab gleichem Startpunkt")
            try:
                ch = hfg.copy()
                ch["timestamp"] = pd.to_datetime(ch["timestamp"], utc=True)
                ch["Serie"] = ch["Coin"].astype(str) + " · " + ch["Variante"].astype(str)
                piv = ch.pivot_table(index="timestamp", columns="Serie", values="Kontowert €", aggfunc="last")
                if len(piv) > 1:
                    st.line_chart(piv)
            except Exception:
                pass
            st.download_button("⬇ Fee-Grid Verlauf CSV", hfg.to_csv(index=False).encode("utf-8-sig"), "fee_aware_grid_verlauf.csv", "text/csv", key="fg_download")

        st.markdown("### Was Fee-Aware anders macht")
        st.write("• gleiche z-Score-Grid-Zielstufen wie das normale V6-Grid")
        st.write("• neue Exposition nur, wenn der Abstand zur 72h-EMA die geschätzten Hin-/Rückkosten plus Puffer übersteigt")
        st.write("• Zieländerung muss zwei abgeschlossene Stunden bestehen")
        st.write("• mindestens 3 Stunden und 0,70% Kursweg zwischen normalen Rebalances")
        st.write("• Risk-off-Ausstiege bleiben sofort möglich")
        st.warning("Shadow-/Paper-Test: Dieser Vergleich verändert weder BTC Grid noch ETH Grid und sendet keine echten Orders.")


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
    st.subheader("🔔 Telegram-Benachrichtigungen")
    st.caption("Optional: V6.8 meldet neue Paper-Trades mit G/V, getrennte 9/10- und 10/10-Quality-Signale, Führungswechsel und eine ausführlichere Tagesübersicht.")
    cfg = notification_settings()
    a, b, c = st.columns(3)
    a.metric("Bot-Token", "✅ gesetzt" if cfg["bot_token_set"] else "❌ fehlt")
    b.metric("Chat-ID", "✅ gesetzt" if cfg["chat_id"] else "❌ fehlt")
    c.metric("Worker-Intervall", f"{os.environ.get('AUTO_UPDATE_MINUTES','60')} Min.")

    if not cfg["bot_token_set"]:
        st.warning("In Railway unter **Variables** zuerst `TELEGRAM_BOT_TOKEN` setzen. Den Token bekommst du in Telegram von **@BotFather** mit `/newbot`. Danach Railway neu deployen.")
    else:
        if not cfg["chat_id"]:
            st.info("Öffne deinen neuen Telegram-Bot, drücke **Start** und sende ihm z. B. `Hallo`. Danach hier die Chat-ID erkennen lassen.")
        else:
            valid_chat, valid_detail = validate_chat_id(cfg["chat_id"])
            if valid_chat:
                st.success(f"Telegram-Ziel geprüft: {valid_detail}.")
            else:
                st.warning(f"Die aktuell gespeicherte Chat-ID ist nicht verwendbar: {valid_detail}")

        col_detect, col_test = st.columns(2)
        if col_detect.button("📲 Chat-ID neu erkennen & speichern", use_container_width=True):
            try:
                cid, label = discover_chat_id()
                st.success(f"Private Chat-ID gespeichert für {label} ({cid}).")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        if col_test.button("🧪 Testnachricht senden", type="primary", use_container_width=True):
            ok, msg = send_telegram("✅ Crypto Trading Zentrale V6.8: Telegram-Benachrichtigungen funktionieren.")
            if ok:
                st.success(msg)
            else:
                st.error(msg)

        with st.expander("Chat-ID manuell eintragen / reparieren"):
            manual_id = st.text_input("Telegram Chat-ID", value="")
            if st.button("Chat-ID prüfen & speichern", key="save_chat_manual"):
                valid, detail = validate_chat_id(manual_id)
                if valid:
                    save_chat_id(manual_id)
                    st.success(f"Chat-ID gespeichert: {detail}.")
                    st.rerun()
                else:
                    st.error(f"Chat-ID nicht gespeichert: {detail}")
            if st.button("Gespeicherte Chat-ID löschen", key="clear_chat_id"):
                clear_saved_chat_id()
                st.success("Gespeicherte Chat-ID gelöscht. Jetzt kannst du sie neu erkennen lassen.")
                st.rerun()

    st.markdown("### Komfort-Tests")
    t1, t2 = st.columns(2)
    if t1.button("📊 Tagesübersicht jetzt senden", use_container_width=True):
        preview = build_daily_summary(
            latest_combined(), read_json(MONITOR_LATEST, {}) or {},
            read_json(V52_LATEST, {}) or {}, read_json(V6_LATEST, {}) or {},
            timezone=str(cfg["timezone"]),
        )
        ok, msg = send_telegram(preview)
        if ok:
            st.success("Tagesübersicht wurde gesendet.")
        else:
            st.error(msg)
    if t2.button("📡 Aktuelles Top-Signal senden", use_container_width=True):
        mon = read_json(MONITOR_LATEST, {}) or {}
        rows_now = mon.get("rows", []) or []
        if not rows_now:
            st.warning("Noch kein Markt-Scan vorhanden.")
        else:
            r = rows_now[0]
            rules = int(r.get("rules_ok", 0) or 0)
            total = int(r.get("rules_total", 10) or 10)
            txt = f"📡 Aktuelles Top-Signal: {r.get('Coin','–')} · {rules}/{total} Regeln · Edge {float(r.get('Edge',0)):.1f} · RSI {float(r.get('RSI',0)):.1f}"
            if r.get("Fehlt"):
                txt += f"\nFehlt: {r.get('Fehlt')}"
            ok, msg = send_telegram(txt)
            if ok:
                st.success("Top-Signal wurde gesendet.")
            else:
                st.error(msg)

    st.markdown("### Was wird automatisch gemeldet?")
    rows = [
        ["Neue Paper-Trade-Aktionen", "✅" if cfg["notify_trades"] else "aus"],
        ["Quality-Warnung bei 9/10", "✅" if (cfg["notify_market"] and cfg["notify_quality_9"]) else "aus"],
        ["Quality-Alarm bei 10/10 / READY", "✅" if (cfg["notify_market"] and cfg["notify_quality_10"]) else "aus"],
        ["Neuer Führender im Paper-Wettkampf", "✅" if cfg["notify_leader"] else "aus"],
        [f"Tagesübersicht ab {cfg['daily_hour']:02d}:00 ({cfg['timezone']})", "✅" if cfg["notify_daily"] else "aus"],
    ]
    st.dataframe(pd.DataFrame(rows, columns=["Meldung", "Status"]), use_container_width=True, hide_index=True)
    st.caption("Diese Schalter können bei Bedarf über Railway-Variablen angepasst werden: `NOTIFY_TRADES`, `NOTIFY_MARKET_CANDIDATES`, `NOTIFY_QUALITY_9`, `NOTIFY_QUALITY_10`, `NOTIFY_LEADER_CHANGE`, `NOTIFY_DAILY_SUMMARY`, `DAILY_SUMMARY_HOUR`.")

    log = notification_log(100)
    if not log.empty:
        st.markdown("### Letzte Benachrichtigungen")
        st.dataframe(log.sort_values("timestamp", ascending=False), use_container_width=True, hide_index=True)
    else:
        st.info("Noch keine Telegram-Nachrichten protokolliert.")

with tab4:
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
    st.write("Broker-API-Schlüssel gehören später ausschließlich in Server-Secrets/Umgebungsvariablen, niemals in ZIP-Dateien oder Quellcode. V6.10 enthält weiterhin keinerlei echte Broker-Orderfunktion; der neue Live-Paper-Modus simuliert Orders ausschließlich mit Spielgeld.")
    st.markdown("### 24/7-Aktualisierung")
    worker = state_status().get("worker") or {}
    if worker:
        st.json(worker)
    else:
        st.info("Auf einem Cloud-Server startet `worker.py` automatisch mit. Standard: eine Aktualisierung pro Stunde.")

with tab5:
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
st.caption("V6.10 Cloud bleibt reines Paper-Trading/Monitoring. Multi-Coin Live-Paper nutzt aktuelle Kurse, sendet aber keinerlei echte Orders.")
