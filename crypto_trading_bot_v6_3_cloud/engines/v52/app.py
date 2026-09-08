from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import streamlit as st

from analytics import analyze_trade_log, coin_breakdown
from backtest import backtest
from data import CRYPTO_UNIVERSE, load_history, load_hourly_history
from scanner import scan_universe, market_regime_from_scan, regime_policy_label
from scanner_backtest import run_scanner_backtest
from optimizer import prepare_snapshots, optimize_parameters
from walkforward import walk_forward_optimize, compare_regime_policies, regime_distribution
from multi_strategy import (STRATEGY_LABELS, STRATEGY_RISK, strategy_comparison, fold_stability, benchmark_buy_hold_btc, benchmark_btc_ema, simulate_multi_strategy)
from breakout_lab import optimize_breakout, baseline_breakout_params, simulate_breakout, breakout_audit, analyze_worst_search_block
from hypothesis_lab import run_hypothesis_test
from rotation_lab import prepare_rotation_snapshots, compare_rotation_strategies, benchmark_rotation_buy_hold, simulate_rotation
from defensive_lab import prepare_defensive_snapshots, compare_defensive_strategies, benchmark_buy_hold as defensive_buy_hold
from forward_competition import evaluate_forward_competition, quality_breakout_frozen_params
from strategies import STRATEGIES

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "scanner_config.json"
STATE_FILE = ROOT / "scanner_state.json"
STATUS_FILE = ROOT / "scanner_status.json"
LOG_FILE = ROOT / "scanner_trades.csv"
STOP_FILE = ROOT / "SCANNER_STOP"
SHADOW_FILE = ROOT / "hypothesis_shadow.json"
ROTATION_SHADOW_FILE = ROOT / "rotation_shadow.json"
DEFENSIVE_SHADOW_FILE = ROOT / "defensive_shadow.json"
FORWARD_COMP_FILE = ROOT / "forward_competition.json"
FORWARD_HISTORY_FILE = ROOT / "forward_competition_history.csv"

st.set_page_config(page_title="Crypto Trading Bot V5.2", page_icon="📈", layout="wide")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def default_state(start_capital: float) -> dict:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "cash": float(start_capital), "positions": {}, "realized_pnl": 0.0,
        "created_at": now, "last_update": now,
        "day_key": datetime.now().astimezone().date().isoformat(),
        "day_start_equity": float(start_capital), "blocked_today": False,
    }


def reset_account(start_capital: float) -> None:
    STOP_FILE.write_text("stop", encoding="utf-8")
    write_json(STATE_FILE, default_state(start_capital))
    for p in [LOG_FILE, STATUS_FILE]:
        if p.exists(): p.unlink()
    if STOP_FILE.exists(): STOP_FILE.unlink()


def start_worker() -> None:
    if STOP_FILE.exists(): STOP_FILE.unlink()
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        [sys.executable, str(ROOT / "scanner_engine.py")], cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags, close_fds=(os.name != "nt"),
    )


def bot_is_alive(status: dict) -> bool:
    if not status.get("running") or STOP_FILE.exists(): return False
    heartbeat = status.get("heartbeat")
    if not heartbeat: return False
    try:
        hb = datetime.fromisoformat(heartbeat)
        return (datetime.now().astimezone() - hb).total_seconds() < 45
    except Exception:
        return False


def score_table(rankings: list[dict]) -> pd.DataFrame:
    if not rankings: return pd.DataFrame()
    rows = []
    for r in rankings:
        rows.append({
            "Rang": 0, "Coin": r.get("asset"), "Score": r.get("score"), "Signal": r.get("signal"),
            "Risiko": r.get("risk_level"), "Preis (€)": r.get("price"), "24h %": r.get("return_24h_pct"),
            "7T %": r.get("return_7d_pct"), "RSI": r.get("rsi"), "ATR %": r.get("atr_pct"),
            "Volumen x": r.get("volume_ratio"), "Hinweis": r.get("note"),
        })
    out = pd.DataFrame(rows).sort_values("Score", ascending=False).reset_index(drop=True)
    out["Rang"] = range(1, len(out)+1)
    return out


@st.cache_data(ttl=900, show_spinner=False)
def cached_hourly(symbol: str, start_iso: str, end_iso: str) -> pd.DataFrame:
    return load_hourly_history(symbol, pd.Timestamp(start_iso).to_pydatetime(), pd.Timestamp(end_iso).to_pydatetime())


saved_cfg = {
    "start_capital": 1000.0, "allocation_pct": 20.0, "stop_loss_pct": 4.0, "take_profit_pct": 8.0,
    "fee_pct": 0.25, "daily_loss_limit_pct": 3.0, "trailing_stop_pct": 3.0,
    "trailing_activation_pct": 4.0, "minimum_score": 70.0, "exit_score": 45.0, "regime_filter": "all",
    "max_positions": 2, "poll_seconds": 300, "strategy_mode": "AUTO_ROUTER",
}
saved_cfg.update(read_json(CONFIG_FILE, {}))

st.title("📈 Crypto Trading Bot – V5.2")
st.caption("Eingefrorener Forward-Paper-Wettkampf + bisherige Forschungstests + Paper-Trading. Keine echten Orders.")

with st.sidebar:
    st.header("Trading-Einstellungen")
    start_capital = st.number_input("Virtuelles Startkapital (€)", 100.0, 100000.0, float(saved_cfg.get("start_capital", 1000.0)), 100.0)
    allocation = st.slider("Positionsgröße (% vom Kontowert)", 5, 50, int(saved_cfg.get("allocation_pct", 20)), 5)
    stop_loss = st.slider("Stop-Loss (%)", 0.0, 25.0, float(saved_cfg.get("stop_loss_pct", 4.0)), 0.5)
    take_profit = st.slider("Take-Profit (%)", 0.0, 50.0, float(saved_cfg.get("take_profit_pct", 8.0)), 0.5)
    trailing_stop = st.slider("Trailing-Stop (%) – 0 = aus", 0.0, 15.0, float(saved_cfg.get("trailing_stop_pct", 3.0)), 0.5)
    trailing_activation = st.slider("Trailing aktiv ab Gewinn (%)", 0.0, 20.0, float(saved_cfg.get("trailing_activation_pct", 4.0)), 0.5)
    fee = st.number_input("Simulierte Gebühr je Order (%)", 0.0, 5.0, float(saved_cfg.get("fee_pct", 0.25)), 0.05)
    daily_loss = st.slider("Tagesverlust-Limit (%)", 0.0, 15.0, float(saved_cfg.get("daily_loss_limit_pct", 3.0)), 0.5)
    st.caption("Trailing-Stop zieht nach, sobald der Aktivierungsgewinn erreicht wurde. Das Tageslimit sperrt nur neue Einstiege.")

scanner_tab, auto_tab, scanner_bt_tab, optimizer_tab, walk_tab, strategy_lab_tab, breakout_lab_tab, hypothesis_tab, rotation_tab, defensive_tab, forward_tab, analytics_tab, backtest_tab, explain_tab = st.tabs([
    "🔎 Markt-Scanner", "🤖 Auto-Scanner Paper", "🧪 Scanner-Backtest", "⚙️ Optimierer", "🧭 Walk-Forward", "🧠 Strategie-Labor", "🎯 Quality-Breakout", "🧬 Hypothesen-Test", "🔄 Relative Stärke", "🛡️ BTC/ETH/Cash", "🏁 Forward-Wettkampf", "📈 Auswertung", "📊 Einzel-Backtest", "❓ So arbeitet V5.2"
])

default_assets = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LINK", "AVAX"]

with scanner_tab:
    st.subheader("Welche Coins sehen nach dem Regelmodell aktuell am interessantesten aus?")
    scan_assets = st.multiselect("Coins scannen", list(CRYPTO_UNIVERSE.keys()), default=default_assets, key="manual_scan_assets")
    if st.button("🔎 Jetzt scannen", type="primary"):
        with st.spinner("Kursdaten werden geladen und bewertet …"):
            st.session_state["manual_rankings"] = [r.to_dict() for r in scan_universe(scan_assets or ["BTC", "ETH"])]
    rankings = st.session_state.get("manual_rankings", [])
    if rankings:
        table = score_table(rankings); valid = table[table["Signal"] != "FEHLER"]
        if not valid.empty:
            best = valid.iloc[0]; c1,c2,c3,c4 = st.columns(4)
            c1.metric("Aktuell #1", str(best["Coin"])); c2.metric("Opportunity-Score", f"{best['Score']:.1f}/100")
            c3.metric("24h", f"{best['24h %']:+.2f} %"); c4.metric("Risiko", str(best["Risiko"]))
            bench = next((r for r in rankings if r.get("asset") == "BTC"), rankings[0] if rankings else None)
            regime_now = market_regime_from_scan(bench)
            st.info(f"{regime_now['emoji']} **Marktphase:** {regime_now['label']} – {regime_now['reason']}")
        try: st.dataframe(table.style.background_gradient(subset=["Score"], cmap="RdYlGn"), use_container_width=True, hide_index=True)
        except ImportError: st.dataframe(table, use_container_width=True, hide_index=True)
        st.info("Der Score ist eine technische Auswahlregel und keine Gewinnprognose.")
    else:
        st.info("Auf ‚Jetzt scannen‘ klicken.")

with auto_tab:
    status = read_json(STATUS_FILE, {}); state = read_json(STATE_FILE, default_state(start_capital))
    hdr1, hdr2 = st.columns([5,1])
    with hdr1:
        st.subheader("Automatischer Markt-Scanner")
    with hdr2:
        st.button("↻ Anzeige aktualisieren", use_container_width=True, help="Aktualisiert nur die Anzeige. Der Paper-Bot läuft unabhängig davon im Hintergrund weiter.")
    st.caption("Der Bot scannt regelmäßig, eröffnet die besten zulässigen Setups und verwaltet Exits automatisch. Die Anzeige aktualisiert sich bewusst nicht mehr automatisch, damit lange Backtests nicht unterbrochen werden.")

    r1,r2,r3,r4 = st.columns([2.4,1,1,1])
    with r1:
        auto_assets = st.multiselect("Universum", list(CRYPTO_UNIVERSE.keys()), default=saved_cfg.get("assets", default_assets), key="auto_assets")
    with r2:
        min_score = st.number_input("Einstieg ab Score", 0.0, 100.0, float(saved_cfg.get("minimum_score", 70.0)), 1.0)
    with r3:
        max_positions = st.selectbox("Max. Positionen", [1,2,3,4,5], index=[1,2,3,4,5].index(int(saved_cfg.get("max_positions", 2))))
    with r4:
        poll_options = [1,2,5,10,15,30,60]; saved_poll = max(1, int(saved_cfg.get("poll_seconds", 300))//60)
        poll_min = st.selectbox("Prüfen alle", poll_options, index=poll_options.index(saved_poll) if saved_poll in poll_options else 2, format_func=lambda x: f"{x} Min.")
    exit_score = st.slider("Score-Exit: verkaufen, wenn Score darunter fällt", 0.0, 80.0, float(saved_cfg.get("exit_score", 45.0)), 1.0)
    regime_filter = st.selectbox(
        "Marktphasen-Filter für neue Einstiege (Legacy-Score)",
        ["all", "no_bear", "bull_only"],
        index=["all", "no_bear", "bull_only"].index(str(saved_cfg.get("regime_filter", "all"))) if str(saved_cfg.get("regime_filter", "all")) in ["all", "no_bear", "bull_only"] else 0,
        format_func=regime_policy_label,
        help="Dieser Filter gilt für den alten Score-Modus. Der Auto-Router entscheidet seine Marktphase selbst und bleibt im Bärenmarkt standardmäßig in Cash.",
    )
    strategy_options = ["AUTO_ROUTER", "MOMENTUM", "BREAKOUT", "PULLBACK", "MEAN_REVERSION", "CASH", "LEGACY_SCORE"]
    strategy_mode = st.selectbox(
        "Handelslogik", strategy_options,
        index=strategy_options.index(str(saved_cfg.get("strategy_mode", "AUTO_ROUTER"))) if str(saved_cfg.get("strategy_mode", "AUTO_ROUTER")) in strategy_options else 0,
        format_func=lambda x: STRATEGY_LABELS.get(x, "Legacy: bisheriger Opportunity-Score"),
        help="Auto-Router: bullisch = Breakout/Momentum/Pullback, neutral = Pullback/Mean-Reversion, bärisch = Cash. Die neuen Strategien haben eigene Stop-/Zielprofile.",
    )
    if strategy_mode != "LEGACY_SCORE":
        st.caption("V4.8-Strategien nutzen eigene Risiko-Profile je Setup. Die seitlichen SL/TP/Trailing-Werte gelten weiterhin für Legacy-Score und ältere Backtests.")

    cfg = {
        "assets": auto_assets or ["BTC", "ETH"], "start_capital": float(start_capital), "allocation_pct": float(allocation),
        "stop_loss_pct": float(stop_loss), "take_profit_pct": float(take_profit), "fee_pct": float(fee),
        "poll_seconds": int(poll_min*60), "minimum_score": float(min_score), "exit_score": float(exit_score),
        "max_positions": int(max_positions), "daily_loss_limit_pct": float(daily_loss),
        "trailing_stop_pct": float(trailing_stop), "trailing_activation_pct": float(trailing_activation),
        "regime_filter": str(regime_filter), "strategy_mode": str(strategy_mode),
    }

    b1,b2,b3 = st.columns(3)
    if b1.button("▶ Auto-Scanner starten", use_container_width=True, type="primary"):
        write_json(CONFIG_FILE, cfg)
        if not STATE_FILE.exists(): write_json(STATE_FILE, default_state(start_capital))
        if bot_is_alive(read_json(STATUS_FILE, {})): st.success("Bot läuft bereits. Neue Einstellungen gelten ab dem nächsten Scan.")
        else: start_worker(); st.success("Auto-Scanner wurde gestartet.")
        st.rerun()
    if b2.button("⏹ Bot stoppen", use_container_width=True):
        STOP_FILE.write_text("stop", encoding="utf-8"); st.info("Stop-Signal gesetzt."); st.rerun()
    if b3.button("↺ Paper-Konto zurücksetzen", use_container_width=True):
        reset_account(start_capital); st.success(f"Paper-Konto auf {start_capital:.2f} € zurückgesetzt."); st.rerun()

    status = read_json(STATUS_FILE, {}); state = read_json(STATE_FILE, default_state(start_capital)); alive = bot_is_alive(status)
    if alive:
        st.success("🟢 AUTO-SCANNER AKTIV")
    else:
        st.warning("⚪ AUTO-SCANNER GESTOPPT")
    active_mode = str(status.get("strategy_mode", read_json(CONFIG_FILE, cfg).get("strategy_mode", strategy_mode)))
    st.caption(f"Aktive Handelslogik: **{STRATEGY_LABELS.get(active_mode, 'Legacy: bisheriger Opportunity-Score')}**")
    live_regime = status.get("market_regime", {}) or {}
    if live_regime:
        phase_txt = f"{live_regime.get('emoji','🟡')} Marktphase: **{live_regime.get('label','Neutral')}** – {live_regime.get('reason','')}"
        if active_mode == "LEGACY_SCORE":
            if status.get("regime_allows_entries", True):
                st.info(phase_txt + f" · Filter: {regime_policy_label(status.get('regime_filter','all'))}")
            else:
                st.warning(phase_txt + f" · **Neueinstiege blockiert** ({regime_policy_label(status.get('regime_filter','all'))})")
        else:
            code = str(live_regime.get("code", "NEUTRAL"))
            allowed_phase = ((active_mode == "AUTO_ROUTER" and code in {"BULL","NEUTRAL"}) or
                             (active_mode in {"MOMENTUM","BREAKOUT"} and code == "BULL") or
                             (active_mode == "PULLBACK" and code in {"BULL","NEUTRAL"}) or
                             (active_mode == "MEAN_REVERSION" and code == "NEUTRAL"))
            if allowed_phase:
                st.info(phase_txt + " · V4.8-Strategie darf in dieser Marktphase nach Setups suchen.")
            else:
                st.warning(phase_txt + " · **Diese Handelslogik bleibt in dieser Marktphase in Cash.**")
    cash = float(state.get("cash", start_capital)); equity = float(status.get("equity", cash)); realized = float(state.get("realized_pnl", 0.0))
    ret = ((equity/start_capital)-1)*100 if start_capital else 0.0; positions = state.get("positions", {}) or {}
    m1,m2,m3,m4,m5 = st.columns(5)
    m1.metric("Kontowert", f"{equity:,.2f} €", f"{ret:+.2f} %"); m2.metric("Freies Guthaben", f"{cash:,.2f} €")
    m3.metric("Realisierter G/V", f"{realized:+,.2f} €"); m4.metric("Offene Positionen", str(len(positions)))
    m5.metric("Letzter Scan", str(status.get("last_check", "–")).replace("T", " ")[:19])
    if status.get("blocked_today"): st.error("🛑 Tagesverlust-Limit erreicht: Bis morgen keine neuen Einstiege.")

    rankings_live = status.get("rankings", []) or []
    if rankings_live:
        st.subheader("Aktuelles Ranking"); table = score_table(rankings_live)
        try: st.dataframe(table.head(12).style.background_gradient(subset=["Score"], cmap="RdYlGn"), use_container_width=True, hide_index=True)
        except ImportError: st.dataframe(table.head(12), use_container_width=True, hide_index=True)

    st.subheader("Offene Positionen – Live-Details")
    if positions:
        score_by_asset = {r.get("asset"): r for r in rankings_live}; active_cfg = read_json(CONFIG_FILE, cfg)
        sl_pct = float(active_cfg.get("stop_loss_pct", stop_loss)); tp_pct = float(active_cfg.get("take_profit_pct", take_profit))
        fee_pct_active = float(active_cfg.get("fee_pct", fee)); exit_score_active = float(active_cfg.get("exit_score", exit_score))
        trail_pct = float(active_cfg.get("trailing_stop_pct", trailing_stop)); trail_activate = float(active_cfg.get("trailing_activation_pct", trailing_activation))
        pos_rows=[]
        for asset,pos in positions.items():
            price = float((status.get("prices", {}) or {}).get(asset, pos.get("last_price", pos["entry"])))
            entry=float(pos["entry"]); qty=float(pos["qty"]); entry_total=float(pos.get("entry_cost_total", qty*entry))
            market_value=qty*price; est_sell_fee=market_value*(fee_pct_active/100.0); pnl_net=(market_value-est_sell_fee)-entry_total
            pnl_pct=(pnl_net/entry_total*100) if entry_total else 0.0
            pos_strategy=str(pos.get("strategy", "LEGACY_SCORE"))
            if pos_strategy in STRATEGY_RISK:
                rp=STRATEGY_RISK[pos_strategy]; pos_sl=float(rp["sl"]); pos_tp=float(rp["tp"]); pos_trail=float(rp["trail"]); pos_trail_activate=float(rp["trail_activate"])
            else:
                pos_sl=sl_pct; pos_tp=tp_pct; pos_trail=trail_pct; pos_trail_activate=trail_activate
            stop_price=entry*(1-pos_sl/100) if pos_sl>0 else None; target_price=entry*(1+pos_tp/100) if pos_tp>0 else None
            peak=max(float(pos.get("peak_price", price)), price)
            trail_level=peak*(1-pos_trail/100) if pos_trail>0 and peak>=entry*(1+pos_trail_activate/100) else None
            scan=score_by_asset.get(asset, {}); current_score=scan.get("score")
            score_delta=(float(current_score)-float(pos.get("entry_score", current_score))) if current_score is not None else None
            st.markdown(f"### {asset} · {STRATEGY_LABELS.get(pos_strategy, 'Legacy Score')}")
            c1,c2,c3,c4,c5,c6,c7=st.columns(7)
            c1.metric("Einstieg", f"{entry:,.4f} €"); c2.metric("Aktuell", f"{price:,.4f} €", f"{((price/entry)-1)*100:+.2f} %" if entry else None)
            c3.metric("G/V bei Verkauf*", f"{pnl_net:+,.2f} €", f"{pnl_pct:+.2f} %")
            c4.metric("Stop-Loss", f"{stop_price:,.4f} €" if stop_price else "aus")
            c5.metric("Take-Profit", f"{target_price:,.4f} €" if target_price else "aus")
            c6.metric("Trailing", f"{trail_level:,.4f} €" if trail_level else "noch inaktiv", f"Peak {peak:,.4f} €")
            c7.metric("Score", f"{float(current_score):.1f}" if current_score is not None else "–", f"{score_delta:+.1f} seit Einstieg" if score_delta is not None else None)
            if stop_price and price<=stop_price: st.warning("Stop-Loss-Niveau erreicht – Exit beim nächsten Bot-Check.")
            elif target_price and price>=target_price: st.success("Take-Profit-Niveau erreicht – Exit beim nächsten Bot-Check.")
            elif trail_level and price<=trail_level: st.warning("Trailing-Stop erreicht – Exit beim nächsten Bot-Check.")
            elif pos_strategy == "LEGACY_SCORE" and current_score is not None and float(current_score)<exit_score_active: st.warning(f"Score unter Exit-Schwelle {exit_score_active:.1f}.")
            pos_rows.append({"Coin":asset,"Strategie":STRATEGY_LABELS.get(pos_strategy, pos_strategy),"Einstieg (€)":entry,"Aktuell (€)":price,"G/V netto (€)":round(pnl_net,2),"G/V netto (%)":round(pnl_pct,2),"Peak (€)":peak,"Trailing (€)":trail_level,"Score":current_score,"Menge":qty})
        with st.expander("Alle Positionsdaten als Tabelle"): st.dataframe(pd.DataFrame(pos_rows), use_container_width=True, hide_index=True)
        st.caption("* enthält die simulierte Verkaufsgebühr. V4.8-Strategien nutzen ihr eigenes Stop-/Ziel-/Trailing-Profil; Legacy-Score nutzt die Seitenleiste.")
    else: st.info("Noch keine Position offen.")
    if status.get("message"): st.caption(status["message"])
    if status.get("error"): st.error(f"Letzter Fehler: {status['error']}")

    st.subheader("Trade-Historie")
    if LOG_FILE.exists():
        try: st.dataframe(pd.read_csv(LOG_FILE).iloc[::-1], use_container_width=True, hide_index=True)
        except Exception as exc: st.warning(f"Trade-Log konnte nicht gelesen werden: {exc}")
    else: st.info("Noch keine Auto-Scanner-Trades vorhanden.")

with scanner_bt_tab:
    st.subheader("Historischer Backtest der kompletten Scanner-Logik")
    st.caption("Hier wird nicht nur ein Coin getestet: Der Scanner rankt mehrere Coins zu jedem historischen Prüfzeitpunkt und wählt selbst die Kandidaten – ohne zukünftige Daten zu verwenden.")
    bt1,bt2,bt3 = st.columns([2.3,1,1])
    with bt1:
        scanner_bt_assets = st.multiselect("Coins für Backtest", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="scanner_bt_assets")
    with bt2:
        bt_days = st.selectbox("Testzeitraum", [30,60,90,180,365], index=2, format_func=lambda x:f"letzte {x} Tage")
    with bt3:
        bt_scan_hours = st.selectbox("Historisch scannen alle", [1,3,6,12,24], index=2, format_func=lambda x:f"{x} Std.")
    st.caption(f"Verwendet deine aktuellen Regeln: Einstieg ≥ {min_score:.0f}, Score-Exit {exit_score:.0f}, SL {stop_loss:.1f} %, TP {take_profit:.1f} %, Trailing {trailing_stop:.1f} % ab +{trailing_activation:.1f} %, Marktphase: {regime_policy_label(regime_filter)}.")
    if st.button("🧪 Scanner-Backtest starten", type="primary"):
        if len(scanner_bt_assets) < 2:
            st.error("Bitte mindestens zwei Coins auswählen.")
        else:
            end_dt=datetime.now(); start_dt=end_dt-timedelta(days=int(bt_days)+40); data={}; failures=[]
            progress=st.progress(0, text="Lade 1h-Kursdaten …")
            for i,asset in enumerate(scanner_bt_assets):
                try: data[asset]=cached_hourly(CRYPTO_UNIVERSE[asset], start_dt.isoformat(), end_dt.isoformat())
                except Exception as exc: failures.append(f"{asset}: {exc}")
                progress.progress((i+1)/len(scanner_bt_assets), text=f"Kursdaten {i+1}/{len(scanner_bt_assets)}")
            progress.empty()
            if failures: st.warning("Einzelne Datenquellen fehlgeschlagen: " + " | ".join(failures[:3]))
            try:
                with st.spinner("Scanner wird historisch durchgespielt …"):
                    res=run_scanner_backtest(data,start_capital=start_capital,allocation_pct=allocation,fee_pct=fee,
                        stop_loss_pct=stop_loss,take_profit_pct=take_profit,minimum_score=min_score,exit_score=exit_score,
                        max_positions=max_positions,daily_loss_limit_pct=daily_loss,trailing_stop_pct=trailing_stop,
                        trailing_activation_pct=trailing_activation,scan_every_hours=bt_scan_hours,test_days=bt_days,
                        regime_policy=regime_filter)
                st.session_state["scanner_bt_result"]=res
                st.success("Scanner-Backtest abgeschlossen – Ergebnisse stehen direkt darunter.")
            except Exception as exc: st.error(f"Scanner-Backtest fehlgeschlagen: {exc}")
    res=st.session_state.get("scanner_bt_result")
    if res is not None:
        pf="∞" if math.isinf(res.profit_factor) else f"{res.profit_factor:.2f}"
        a,b,c,d,e,f=st.columns(6)
        a.metric("Endwert",f"{res.final_value:,.2f} €"); b.metric("Rendite",f"{res.return_pct:+.2f} %")
        c.metric("Max. Drawdown",f"-{res.max_drawdown_pct:.2f} %"); d.metric("Abgeschl. Trades",str(res.completed_trades))
        e.metric("Trefferquote",f"{res.win_rate_pct:.1f} %"); f.metric("Profit Factor",pf)
        st.line_chart(res.equity.to_frame(), height=330)
        st.caption(f"Simulierte Gebühren gesamt: {res.total_fees:.2f} €. Offene Positionen werden am Backtest-Ende geschlossen, damit Endwerte vergleichbar sind.")
        if not res.trade_log.empty: st.dataframe(res.trade_log.iloc[::-1], use_container_width=True, hide_index=True)
        st.warning("Ein Backtest kann durch Datenqualität, Slippage und Marktänderungen besser aussehen als späteres Live-Trading. Er ist ein Filter, kein Gewinnbeweis.")

with optimizer_tab:
    st.subheader("Parameter-Optimierer – stabile Regeln statt nur höchster Einzelgewinn")
    st.caption("Der Optimierer berechnet die historischen Scanner-Scores nur einmal und testet danach viele Regelkombinationen. Die ersten 70 % dienen als Training, die letzten 30 % als getrennte Validierung. V4.8 testet zusätzlich, ob Neueinstiege in allen Marktphasen, ohne Bärenmarkt oder nur bullisch besser funktionieren.")

    o1,o2,o3,o4 = st.columns([2.2,1,1,1])
    with o1:
        opt_assets = st.multiselect("Coins optimieren", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="opt_assets")
    with o2:
        opt_days = st.selectbox("Zeitraum", [90,180,365], index=1, format_func=lambda x:f"letzte {x} Tage", key="opt_days")
    with o3:
        opt_scan_hours = st.selectbox("Historisch scannen alle", [3,6,12,24], index=1, format_func=lambda x:f"{x} Std.", key="opt_scan_hours")
    with o4:
        opt_trials = st.selectbox("Kombinationen", [60,120,250], index=1, format_func=lambda x:f"{x} Tests", key="opt_trials")

    st.info("Getestet werden Einstiegsscore, Score-Exit, Stop-Loss, Take-Profit, Trailing-Stop, Trailing-Aktivierung **und der Marktphasen-Filter**. Positionsgröße, Gebühren, Positionslimit und Tagesverlust-Limit bleiben unverändert.")

    if st.button("⚙️ Optimierung starten", type="primary"):
        if len(opt_assets) < 2:
            st.error("Bitte mindestens zwei Coins auswählen.")
        else:
            end_dt = datetime.now(); start_dt = end_dt - timedelta(days=int(opt_days)+40)
            data = {}; failures = []
            progress = st.progress(0, text="Lade 1h-Kursdaten …")
            for i, asset in enumerate(opt_assets):
                try:
                    data[asset] = cached_hourly(CRYPTO_UNIVERSE[asset], start_dt.isoformat(), end_dt.isoformat())
                except Exception as exc:
                    failures.append(f"{asset}: {exc}")
                progress.progress((i+1)/len(opt_assets), text=f"Kursdaten {i+1}/{len(opt_assets)}")
            progress.empty()
            if failures:
                st.warning("Einzelne Datenquellen fehlgeschlagen: " + " | ".join(failures[:3]))
            try:
                prep = st.progress(0, text="Historische Scanner-Scores werden vorbereitet …")
                snapshots = prepare_snapshots(
                    data, test_days=int(opt_days), scan_every_hours=int(opt_scan_hours),
                    progress_cb=lambda x: prep.progress(float(x), text=f"Scanner-Scores vorbereiten: {int(x*100)} %")
                )
                prep.empty()
                optp = st.progress(0, text="Regelkombinationen werden getestet …")
                current_params = {
                    "minimum_score": float(min_score), "exit_score": float(exit_score),
                    "stop_loss_pct": float(stop_loss), "take_profit_pct": float(take_profit),
                    "trailing_stop_pct": float(trailing_stop), "trailing_activation_pct": float(trailing_activation),
                    "regime_policy": str(regime_filter),
                }
                results = optimize_parameters(
                    snapshots, n_trials=int(opt_trials), start_capital=float(start_capital),
                    allocation_pct=float(allocation), fee_pct=float(fee), max_positions=int(max_positions),
                    daily_loss_limit_pct=float(daily_loss), current_params=current_params,
                    progress_cb=lambda x: optp.progress(float(x), text=f"Kombinationen testen: {int(x*100)} %"),
                )
                optp.empty()
                st.session_state["optimizer_results"] = results
                st.success(f"Optimierung abgeschlossen – {len(results)} Regelkombinationen verglichen.")
            except Exception as exc:
                st.error(f"Optimierung fehlgeschlagen: {exc}")

    opt_res = st.session_state.get("optimizer_results")
    if isinstance(opt_res, pd.DataFrame) and not opt_res.empty:
        best = opt_res.iloc[0]
        a,b,c,d,e = st.columns(5)
        a.metric("Beste Validierung", f"{best['Validierung %']:+.2f} %")
        b.metric("Validierungs-Drawdown", f"-{best['Validierung DD %']:.2f} %")
        pf_txt = "∞" if float(best['Validierung PF']) >= 99 else f"{best['Validierung PF']:.2f}"
        c.metric("Validierungs-PF", pf_txt)
        d.metric("Trades Validierung", str(int(best['Validierung Trades'])))
        e.metric("Stabilitäts-Score", f"{best['Stabilitäts-Score']:.2f}")

        st.markdown("#### Aktuell bestplatzierte Regelkombination")
        st.write(
            f"**Einstieg ab Score {best['Entry Score']:.0f} · Score-Exit {best['Exit Score']:.0f} · "
            f"Stop-Loss {best['SL %']:.1f} % · Take-Profit {best['TP %']:.1f} % · "
            f"Trailing {best['Trail %']:.1f} % ab +{best['Trail ab %']:.1f} % · "
            f"Marktphase: {regime_policy_label(best.get('Regime-Filter','all'))}**"
        )
        st.caption("Der Stabilitäts-Score ist nur eine interne Rangzahl. Er ist keine erwartete Rendite. Die Validierung wurde bei der Suche getrennt vom Training ausgewertet, um Überanpassung etwas zu begrenzen.")

        top = opt_res.head(20).copy()
        top["Validierung PF"] = top["Validierung PF"].replace(99.0, float("inf"))
        st.dataframe(top, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇ Optimierer-Ergebnisse als CSV",
            data=opt_res.to_csv(index=False).encode("utf-8-sig"),
            file_name="v4_6_optimizer_results.csv", mime="text/csv",
        )
        distance = (
            (opt_res["Entry Score"]-float(min_score)).abs() +
            (opt_res["Exit Score"]-float(exit_score)).abs() +
            (opt_res["SL %"]-float(stop_loss)).abs() +
            (opt_res["TP %"]-float(take_profit)).abs() +
            (opt_res["Trail %"]-float(trailing_stop)).abs() +
            (opt_res["Trail ab %"]-float(trailing_activation)).abs() +
            (opt_res["Regime-Filter"].astype(str) != str(regime_filter)).astype(float) * 100.0
        )
        current_row = opt_res.iloc[distance.argmin()]
        st.markdown("#### Vergleich mit deinen aktuellen Regeln")
        x,y,z = st.columns(3)
        x.metric("Aktuelle Regeln – Validierung", f"{current_row['Validierung %']:+.2f} %")
        y.metric("Aktuelle Regeln – DD", f"-{current_row['Validierung DD %']:.2f} %")
        z.metric("Aktuelle Regeln – Rang", f"{int(current_row['Rang'])} / {len(opt_res)}")
        st.warning("Nicht einfach blind die Nr. 1 live handeln. Sinnvoll ist, die Top-5 anschließend nochmals auf einem anderen Zeitraum bzw. weiter im Paper-Trading zu vergleichen. Das reduziert das Risiko, nur einen historischen Zufall zu optimieren.")
    else:
        st.info("Wähle Coins und Zeitraum und starte die Optimierung. Für den ersten Lauf sind 180 Tage, 5 Coins, 6 Stunden und 120 Kombinationen ein guter Kompromiss.")


with walk_tab:
    st.subheader("Walk-Forward-Test – Regeln mehrfach auf ungesehenen Zeitabschnitten prüfen")
    st.caption("V4.8 teilt die Historie in sechs Blöcke. Zuerst werden Regeln nur auf den früheren Blöcken optimiert; anschließend wird die gewählte Regel auf dem direkt folgenden, vorher ungesehenen Block getestet. Danach wächst das Trainingsfenster weiter. So entstehen mehrere echte Out-of-Sample-Prüfungen statt nur eines 70/30-Splits.")

    w1,w2,w3,w4 = st.columns([2.2,1,1,1])
    with w1:
        wf_assets = st.multiselect("Coins Walk-Forward", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="wf_assets")
    with w2:
        wf_days = st.selectbox("Zeitraum", [180,365], index=1, format_func=lambda x:f"letzte {x} Tage", key="wf_days")
    with w3:
        wf_scan_hours = st.selectbox("Historisch scannen alle", [6,12,24], index=0, format_func=lambda x:f"{x} Std.", key="wf_scan_hours")
    with w4:
        wf_trials = st.selectbox("Tests je Fold", [40,60,120], index=1, format_func=lambda x:f"{x} Kombinationen", key="wf_trials")

    st.info("Der Walk-Forward-Test darf in jedem Fold seine Regeln nur aus der Vergangenheit auswählen. Getestet werden dabei auch die drei Marktphasen-Varianten: alle Phasen, Bärenmarkt meiden oder nur bullisch.")

    if st.button("🧭 Walk-Forward starten", type="primary"):
        if len(wf_assets) < 2:
            st.error("Bitte mindestens zwei Coins auswählen.")
        else:
            end_dt = datetime.now(); start_dt = end_dt - timedelta(days=int(wf_days)+40)
            data = {}; failures = []
            progress = st.progress(0, text="Lade 1h-Kursdaten …")
            for i, asset in enumerate(wf_assets):
                try:
                    data[asset] = cached_hourly(CRYPTO_UNIVERSE[asset], start_dt.isoformat(), end_dt.isoformat())
                except Exception as exc:
                    failures.append(f"{asset}: {exc}")
                progress.progress((i+1)/len(wf_assets), text=f"Kursdaten {i+1}/{len(wf_assets)}")
            progress.empty()
            if failures:
                st.warning("Einzelne Datenquellen fehlgeschlagen: " + " | ".join(failures[:3]))
            try:
                prep = st.progress(0, text="Historische Scanner-Scores + Marktphasen werden vorbereitet …")
                snapshots = prepare_snapshots(
                    data, test_days=int(wf_days), scan_every_hours=int(wf_scan_hours),
                    progress_cb=lambda x: prep.progress(float(x), text=f"Historie vorbereiten: {int(x*100)} %")
                )
                prep.empty()
                current_params = {
                    "minimum_score": float(min_score), "exit_score": float(exit_score),
                    "stop_loss_pct": float(stop_loss), "take_profit_pct": float(take_profit),
                    "trailing_stop_pct": float(trailing_stop), "trailing_activation_pct": float(trailing_activation),
                    "regime_policy": str(regime_filter),
                }
                prog = st.progress(0, text="Walk-Forward-Folds werden optimiert und getestet …")
                wf_df, wf_summary = walk_forward_optimize(
                    snapshots, n_trials=int(wf_trials), start_capital=float(start_capital),
                    allocation_pct=float(allocation), fee_pct=float(fee), max_positions=int(max_positions),
                    daily_loss_limit_pct=float(daily_loss), current_params=current_params, n_blocks=6,
                    progress_cb=lambda x: prog.progress(float(x), text=f"Walk-Forward: {int(x*100)} %")
                )
                prog.empty()
                regime_cmp = compare_regime_policies(
                    snapshots, params=current_params, start_capital=float(start_capital),
                    allocation_pct=float(allocation), fee_pct=float(fee), max_positions=int(max_positions),
                    daily_loss_limit_pct=float(daily_loss),
                )
                st.session_state["wf_results"] = wf_df
                st.session_state["wf_summary"] = wf_summary
                st.session_state["wf_regime_compare"] = regime_cmp
                st.session_state["wf_regime_dist"] = regime_distribution(snapshots)
                st.success("Walk-Forward abgeschlossen – mehrere ungesehene Zeitblöcke wurden geprüft.")
            except Exception as exc:
                st.error(f"Walk-Forward fehlgeschlagen: {exc}")

    wf_df = st.session_state.get("wf_results")
    wf_summary = st.session_state.get("wf_summary")
    if isinstance(wf_df, pd.DataFrame) and not wf_df.empty and wf_summary is not None:
        if wf_summary.status == "ROBUST":
            st.success(f"🟢 Ergebnis: **ROBUST** – {wf_summary.profitable_folds}/{wf_summary.folds} Testabschnitte waren profitabel.")
        elif wf_summary.status == "GEMISCHT":
            st.warning(f"🟡 Ergebnis: **GEMISCHT** – {wf_summary.profitable_folds}/{wf_summary.folds} Testabschnitte waren profitabel.")
        else:
            st.error(f"🔴 Ergebnis: **INSTABIL** – nur {wf_summary.profitable_folds}/{wf_summary.folds} Testabschnitte waren profitabel.")

        a,b,c,d,e,f = st.columns(6)
        a.metric("Profitable Folds", f"{wf_summary.profitable_folds}/{wf_summary.folds}", f"{wf_summary.profitable_share_pct:.0f} %")
        b.metric("Kombiniert*", f"{wf_summary.compounded_return_pct:+.2f} %")
        c.metric("Ø Test", f"{wf_summary.mean_test_return_pct:+.2f} %")
        d.metric("Schlechtester Test", f"{wf_summary.worst_test_return_pct:+.2f} %")
        e.metric("Max. Test-DD", f"-{wf_summary.max_test_drawdown_pct:.2f} %")
        f.metric("Ø Test-PF", f"{wf_summary.average_test_pf:.2f}", f"{wf_summary.total_test_trades} Trades")
        st.caption("* 'Kombiniert' verknüpft nur die prozentualen Ergebnisse der ungesehenen Testblöcke rechnerisch. Die Folds sind kein durchgehendes Echtgeldkonto.")

        st.markdown("#### Jeder ungesehene Testabschnitt")
        st.dataframe(wf_df, use_container_width=True, hide_index=True)
        st.download_button("⬇ Walk-Forward als CSV", data=wf_df.to_csv(index=False).encode("utf-8-sig"), file_name="v4_6_walk_forward.csv", mime="text/csv")

        dist = st.session_state.get("wf_regime_dist")
        cmp = st.session_state.get("wf_regime_compare")
        st.markdown("#### Marktphasen im Testzeitraum")
        if isinstance(dist, pd.DataFrame) and not dist.empty:
            st.dataframe(dist, use_container_width=True, hide_index=True)
        st.markdown("#### Was bringt der Marktphasen-Filter bei deinen aktuellen Regeln?")
        if isinstance(cmp, pd.DataFrame) and not cmp.empty:
            cmp_show = cmp.copy(); cmp_show["Profit Factor"] = cmp_show["Profit Factor"].replace(99.0, float("inf"))
            st.dataframe(cmp_show, use_container_width=True, hide_index=True)
        st.warning("Auch ein ROBUST-Label ist kein Gewinnversprechen. Es heißt nur, dass die Regeln in dieser historischen Walk-Forward-Prüfung weniger stark von einem einzelnen Zeitabschnitt abhingen.")
    else:
        st.info("Für den ersten belastbareren Lauf: 365 Tage, BTC/ETH/SOL/LINK/AVAX, 6 Stunden und 60 Tests je Fold. Das kann deutlich länger dauern als der normale Optimierer.")


with strategy_lab_tab:
    st.subheader("Strategie-Labor – unterschiedliche Handelsideen statt nur Score-Grenzen")
    st.caption("Hier treten Trend/Momentum, Breakout, Pullback, Mean-Reversion und der automatische Marktphasen-Router gegeneinander an. Zusätzlich siehst du zwei einfache Benchmarks: BTC kaufen & halten sowie BTC EMA 50/200.")

    l1,l2,l3 = st.columns([2.2,1,1])
    with l1:
        lab_assets = st.multiselect("Coins Strategie-Labor", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="lab_assets")
    with l2:
        lab_days = st.selectbox("Zeitraum", [180,365,540], index=1, format_func=lambda x:f"letzte {x} Tage", key="lab_days")
    with l3:
        lab_scan_hours = st.selectbox("Historisch scannen alle", [6,12,24], index=0, format_func=lambda x:f"{x} Std.", key="lab_scan_hours")

    st.info("Ziel ist nicht der höchste Einzelgewinn. Interessant sind Strategien, die in mehreren Zeitblöcken positiv bleiben und einfache Benchmarks schlagen, ohne extremen Drawdown.")
    if st.button("🧠 Strategien vergleichen", type="primary"):
        if len(lab_assets) < 2 or "BTC" not in lab_assets:
            st.error("Bitte mindestens zwei Coins wählen und BTC als Benchmark enthalten.")
        else:
            end_dt = datetime.now(); start_dt = end_dt - timedelta(days=int(lab_days)+40)
            data = {}; failures=[]
            progress = st.progress(0, text="Lade 1h-Kursdaten …")
            for i, asset in enumerate(lab_assets):
                try:
                    data[asset] = cached_hourly(CRYPTO_UNIVERSE[asset], start_dt.isoformat(), end_dt.isoformat())
                except Exception as exc:
                    failures.append(f"{asset}: {exc}")
                progress.progress((i+1)/len(lab_assets), text=f"Kursdaten {i+1}/{len(lab_assets)}")
            progress.empty()
            if failures:
                st.warning("Einzelne Datenquellen fehlgeschlagen: " + " | ".join(failures[:3]))
            try:
                prep = st.progress(0, text="Historische Strategie-Merkmale vorbereiten …")
                snapshots = prepare_snapshots(data, test_days=int(lab_days), scan_every_hours=int(lab_scan_hours),
                    progress_cb=lambda x: prep.progress(float(x), text=f"Historie vorbereiten: {int(x*100)} %"))
                prep.empty()
                common = dict(start_capital=float(start_capital), allocation_pct=float(allocation), fee_pct=float(fee),
                              max_positions=int(saved_cfg.get("max_positions", 2)), daily_loss_limit_pct=float(daily_loss))
                comp = strategy_comparison(snapshots, **common)
                bh = benchmark_buy_hold_btc(snapshots, start_capital=float(start_capital), fee_pct=float(fee))
                ema = benchmark_btc_ema(snapshots, start_capital=float(start_capital), fee_pct=float(fee))
                benchmark_df = pd.DataFrame([bh, ema])
                folds = fold_stability(snapshots, n_blocks=4, **common)
                router_result = simulate_multi_strategy(snapshots, mode="AUTO_ROUTER", **common)
                st.session_state["lab_comparison"] = comp
                st.session_state["lab_benchmarks"] = benchmark_df
                st.session_state["lab_folds"] = folds
                st.session_state["lab_router_equity"] = router_result.equity
                st.session_state["lab_router_trades"] = router_result.trade_log
                st.success("Strategie-Vergleich abgeschlossen.")
            except Exception as exc:
                st.error(f"Strategie-Labor fehlgeschlagen: {exc}")

    comp = st.session_state.get("lab_comparison")
    bench = st.session_state.get("lab_benchmarks")
    folds = st.session_state.get("lab_folds")
    if isinstance(comp, pd.DataFrame) and not comp.empty:
        st.markdown("#### 1) Direkter Vergleich über den gesamten Zeitraum")
        show = comp.copy(); show["Profit Factor"] = show["Profit Factor"].replace(99.0, float("inf"))
        st.dataframe(show, use_container_width=True, hide_index=True)
        if isinstance(bench, pd.DataFrame) and not bench.empty:
            st.markdown("#### 2) Einfache Benchmarks")
            bshow = bench.copy(); bshow["Profit Factor"] = bshow["Profit Factor"].replace(99.0, float("inf"))
            st.dataframe(bshow, use_container_width=True, hide_index=True)
        if isinstance(folds, pd.DataFrame) and not folds.empty:
            st.markdown("#### 3) Stabilität in vier getrennten Zeitblöcken")
            st.dataframe(folds, use_container_width=True, hide_index=True)
            robust = folds[folds["Status"] == "ROBUST"]
            if not robust.empty:
                st.success("🟢 Mindestens eine Strategie erfüllt die aktuelle ROBUST-Regel. Trotzdem weiter Paper-Trading und längere Zeiträume prüfen.")
            else:
                st.warning("Noch keine Strategie ist über vier Zeitblöcke robust. Das ist ein Signal, nicht einfach die beste Gesamt-Rendite live zu übernehmen.")
        eq = st.session_state.get("lab_router_equity")
        if isinstance(eq, pd.Series) and not eq.empty:
            st.markdown("#### Auto-Router – Kontoverlauf")
            st.line_chart(eq)
        trades = st.session_state.get("lab_router_trades")
        if isinstance(trades, pd.DataFrame) and not trades.empty:
            st.markdown("#### Auto-Router – letzte Trades")
            st.dataframe(trades.tail(30), use_container_width=True, hide_index=True)
    else:
        st.info("Für den ersten Lauf: BTC, ETH, SOL, LINK, AVAX · 365 Tage · 6 Stunden.")


with breakout_lab_tab:
    st.subheader("Quality-Breakout – weniger Trades, dafür strengere Einstiege")
    st.caption("V4.8 sucht bewusst nach weniger, hochwertigeren Breakouts und zerlegt anschließend den schwächsten Suchblock. Die älteren 80 % werden in vier Suchblöcke geteilt; die neuesten 20 % bleiben als Holdout unangetastet. Das Ranking belohnt vor allem 3–4 profitable Suchblöcke, niedrigen Drawdown und ausreichenden Profit Factor.")

    bl1, bl2, bl3, bl4 = st.columns([2.2, 1, 1, 1])
    with bl1:
        breakout_assets = st.multiselect("Coins Quality-Breakout", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="breakout_lab_assets")
    with bl2:
        breakout_days = st.selectbox("Zeitraum", [180,365,540], index=1, format_func=lambda x:f"letzte {x} Tage", key="breakout_lab_days")
    with bl3:
        breakout_scan_hours = st.selectbox("Historisch scannen alle", [3,6,12], index=1, format_func=lambda x:f"{x} Std.", key="breakout_lab_scan_hours")
    with bl4:
        breakout_trials = st.selectbox("Regelkombinationen", [120,250,500], index=1, format_func=lambda x:f"{x} Tests", key="breakout_lab_trials")

    st.info("Getestet werden u. a. stärkere Breakout-/Trend-/Volumen-Schwellen, Mindest-Edge, 1–3 Bestätigungs-Scans, engerer Hoch-Abstand, RSI/Momentum, BTC-Marktphase, Stop/TP/Trailing, Mindesthaltezeit sowie Cooldowns. Ziel: **3–4 von 4 Suchblöcken positiv**, PF ≥ 1,2 und DD möglichst < 8 %, danach positiver Holdout.")

    if st.button("🎯 Quality-Breakout starten", type="primary"):
        if len(breakout_assets) < 2 or "BTC" not in breakout_assets:
            st.error("Bitte mindestens zwei Coins wählen und BTC als Marktphasen-Benchmark enthalten.")
        else:
            end_dt = datetime.now(); start_dt = end_dt - timedelta(days=int(breakout_days)+40)
            data = {}; failures=[]
            progress = st.progress(0, text="Lade 1h-Kursdaten …")
            for i, asset in enumerate(breakout_assets):
                try:
                    data[asset] = cached_hourly(CRYPTO_UNIVERSE[asset], start_dt.isoformat(), end_dt.isoformat())
                except Exception as exc:
                    failures.append(f"{asset}: {exc}")
                progress.progress((i+1)/len(breakout_assets), text=f"Kursdaten {i+1}/{len(breakout_assets)}")
            progress.empty()
            if failures:
                st.warning("Einzelne Datenquellen fehlgeschlagen: " + " | ".join(failures[:3]))
            try:
                prep = st.progress(0, text="Historische Breakout-Merkmale vorbereiten …")
                snapshots = prepare_snapshots(data, test_days=int(breakout_days), scan_every_hours=int(breakout_scan_hours),
                    progress_cb=lambda x: prep.progress(float(x), text=f"Historie vorbereiten: {int(x*100)} %"))
                prep.empty()
                opt_progress = st.progress(0, text="Quality-Regeln über vier Suchblöcke testen …")
                results, summary, holdout_result, holdout_top = optimize_breakout(
                    snapshots, n_trials=int(breakout_trials), start_capital=float(start_capital),
                    allocation_pct=float(allocation), fee_pct=float(fee), max_positions=int(max_positions),
                    daily_loss_limit_pct=float(daily_loss), n_search_blocks=4, holdout_fraction=0.20,
                    progress_cb=lambda x: opt_progress.progress(float(x), text=f"Quality-Breakout: {int(x*100)} %")
                )
                opt_progress.empty()
                # Baseline on the exact same full history for orientation only.
                baseline = simulate_breakout(snapshots, params=baseline_breakout_params(), start_capital=float(start_capital),
                    allocation_pct=float(allocation), fee_pct=float(fee), max_positions=int(max_positions),
                    daily_loss_limit_pct=float(daily_loss))
                diag_progress = st.progress(0, text="Schlechtesten Suchblock zerlegen …")
                try:
                    worst_diag = analyze_worst_search_block(
                        snapshots, params=summary.get("best_params", {}), start_capital=float(start_capital),
                        allocation_pct=float(allocation), fee_pct=float(fee), max_positions=int(max_positions),
                        daily_loss_limit_pct=float(daily_loss), n_search_blocks=4, holdout_fraction=0.20
                    )
                    diag_progress.progress(1.0, text="Verlustanalyse abgeschlossen")
                except Exception as diag_exc:
                    worst_diag = {"error": str(diag_exc)}
                finally:
                    diag_progress.empty()
                st.session_state["breakout_results"] = results
                st.session_state["breakout_summary"] = summary
                st.session_state["breakout_holdout_result"] = holdout_result
                st.session_state["breakout_holdout_top"] = holdout_top
                st.session_state["breakout_baseline"] = baseline
                st.session_state["breakout_worst_diag"] = worst_diag
                st.success("Quality-Breakout abgeschlossen. Holdout und Verlustanalyse wurden erst nach dem Such-Ranking berechnet.")
            except Exception as exc:
                st.error(f"Quality-Breakout fehlgeschlagen: {exc}")

    breakout_results = st.session_state.get("breakout_results")
    breakout_summary = st.session_state.get("breakout_summary")
    if isinstance(breakout_results, pd.DataFrame) and not breakout_results.empty and isinstance(breakout_summary, dict):
        status = breakout_summary.get("status", "INSTABIL")
        if status == "ROBUST-KANDIDAT":
            st.success("🟢 **ROBUST-KANDIDAT für längeres Paper-Trading** – Suchblöcke und ungesehener Holdout erfüllen die aktuellen Mindestkriterien. Kein Live-Freibrief.")
        elif status == "GEMISCHT":
            st.warning("🟡 **GEMISCHT** – interessante Breakout-Regeln gefunden, aber Stabilität/Holdout reichen noch nicht für einen robusten Kandidaten.")
        else:
            st.error("🔴 **INSTABIL** – auch der strengere Quality-Breakout liefert noch keinen ausreichend stabilen Kandidaten.")

        a,b,c,d,e,f,g = st.columns(7)
        a.metric("Positive Suchblöcke", breakout_summary.get("positive_search_blocks","–"))
        b.metric("Such-Kombiniert", f"{breakout_summary.get('search_combined_pct',0):+.2f} %")
        c.metric("Schlechtester Block", f"{breakout_summary.get('search_worst_pct',0):+.2f} %")
        d.metric("Max Such-DD", f"-{breakout_summary.get('search_max_dd_pct',0):.2f} %")
        e.metric("Ø Such-PF", f"{breakout_summary.get('search_mean_pf',0):.2f}")
        f.metric("Holdout", f"{breakout_summary.get('holdout_return_pct',0):+.2f} %")
        g.metric("Holdout PF", f"{breakout_summary.get('holdout_pf',0):.2f}", f"{breakout_summary.get('holdout_trades',0)} Trades")

        p = breakout_summary.get("best_params", {})
        st.markdown("#### Bestplatzierte Quality-Regeln – **nur nach den älteren Suchblöcken gerankt**")
        st.write(
            f"Breakout ≥ **{p.get('min_breakout_score','–')}** · Trend ≥ **{p.get('min_trend_score','–')}** · "
            f"Volumen ≥ **{p.get('min_volume_ratio','–')}×** · Momentum ≥ **{p.get('min_momentum_score','–')}** · Edge ≥ **{p.get('min_edge_score','–')}** · Bestätigungen **{p.get('confirmation_scans','–')}** · "
            f"RSI ≤ **{p.get('rsi_max','–')}** · 24h ≥ **{p.get('min_return_24h','–')} %** · "
            f"Abstand zum 30-Tage-Hoch **{p.get('high_distance_min','–')} bis {p.get('high_distance_max','–')} %** · "
            f"Marktfilter **{regime_policy_label(p.get('regime_policy','all'))}** · SL **{p.get('stop_loss_pct','–')} %** · "
            f"TP **{p.get('take_profit_pct','–')} %** · Trailing **{p.get('trailing_stop_pct','–')} % ab +{p.get('trailing_activation_pct','–')} % · "
            f"Signal-Exit frühestens **{p.get('signal_exit_min_hold_hours','–')} h** · Max-Haltedauer **{p.get('max_holding_hours','–')} h** · Exit-Cooldown **{p.get('cooldown_after_exit_hours','–')} h** · Verlust-Cooldown **{p.get('cooldown_loss_hours','–')} h**."
        )
        st.caption("Der Holdout beeinflusst die Rangfolge nicht. Er ist eine nachgelagerte Kontrolle auf dem neuesten, bei der Suche nicht benutzten Zeitabschnitt.")

        baseline = st.session_state.get("breakout_baseline")
        if baseline is not None:
            st.markdown("#### Orientierung: bisheriges V4.5-Breakout-Profil über den Gesamtzeitraum")
            q1,q2,q3,q4 = st.columns(4)
            q1.metric("Baseline Rendite", f"{baseline.return_pct:+.2f} %")
            q2.metric("Baseline DD", f"-{baseline.max_drawdown_pct:.2f} %")
            q3.metric("Baseline PF", "∞" if math.isinf(baseline.profit_factor) else f"{baseline.profit_factor:.2f}")
            q4.metric("Baseline Trades", str(baseline.completed_trades))

        st.markdown("#### Top-Regeln")
        show_cols = [c for c in breakout_results.columns if c not in []]
        st.dataframe(breakout_results.head(25)[show_cols], use_container_width=True, hide_index=True)
        st.download_button("⬇ Quality-Breakout als CSV", data=breakout_results.to_csv(index=False).encode("utf-8-sig"), file_name="v4_9_quality_breakout.csv", mime="text/csv")

        worst_diag = st.session_state.get("breakout_worst_diag")
        if isinstance(worst_diag, dict) and worst_diag.get("error"):
            st.warning(f"Die Quality-Ergebnisse sind gültig, aber die zusätzliche Verlustanalyse konnte nicht aufgebaut werden: {worst_diag.get('error')}")
        if isinstance(worst_diag, dict) and worst_diag.get("worst_result") is not None:
            st.markdown("---")
            st.markdown("### 🔬 Warum verliert der schlechteste Suchblock?")
            st.caption("V4.8 untersucht nur den schwächsten der vier älteren Suchblöcke. Der ungesehene Holdout bleibt aus dieser Ursachenanalyse heraus, damit wir nicht rückwirkend auf den neuesten Abschnitt optimieren.")

            overview = worst_diag.get("block_overview")
            if isinstance(overview, pd.DataFrame) and not overview.empty:
                st.markdown("#### Alle vier Suchblöcke im direkten Vergleich")
                st.dataframe(overview, use_container_width=True, hide_index=True)

            wr = worst_diag.get("worst_result")
            block_no = int(worst_diag.get("worst_block_number", 0) or 0)
            wfrom = worst_diag.get("worst_from")
            wto = worst_diag.get("worst_to")
            d1,d2,d3,d4,d5 = st.columns(5)
            d1.metric("Schlechtester Block", f"#{block_no}", f"{pd.Timestamp(wfrom).date()} – {pd.Timestamp(wto).date()}" if wfrom is not None and wto is not None else None)
            d2.metric("Rendite", f"{wr.return_pct:+.2f} %")
            d3.metric("Drawdown", f"-{wr.max_drawdown_pct:.2f} %")
            d4.metric("Profit Factor", "∞" if math.isinf(wr.profit_factor) else f"{wr.profit_factor:.2f}")
            d5.metric("Trades", str(wr.completed_trades), f"max. Verlustserie {worst_diag.get('max_losing_streak',0)}")

            notes = worst_diag.get("notes", [])
            if notes:
                st.markdown("#### Auffälligkeiten")
                for note in notes:
                    st.write(f"• {note}")

            diag_tabs = st.tabs(["Coins", "Exit-Gründe", "Marktphase", "Entry-Merkmale", "Haltedauer / Wochentag", "Trade-Liste"])
            with diag_tabs[0]:
                t = worst_diag.get("coin_breakdown")
                if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
                else: st.info("Zu wenige Trades für eine Coin-Aufschlüsselung.")
            with diag_tabs[1]:
                t = worst_diag.get("reason_breakdown")
                if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
                else: st.info("Keine Exit-Aufschlüsselung verfügbar.")
            with diag_tabs[2]:
                t = worst_diag.get("regime_breakdown")
                if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
                else: st.info("Keine Marktphasen-Aufschlüsselung verfügbar.")
            with diag_tabs[3]:
                c_left, c_mid, c_right = st.columns(3)
                with c_left:
                    st.markdown("**RSI beim Einstieg**")
                    t = worst_diag.get("rsi_breakdown")
                    if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
                with c_mid:
                    st.markdown("**Volumen beim Einstieg**")
                    t = worst_diag.get("volume_breakdown")
                    if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
                with c_right:
                    st.markdown("**Breakout-Score beim Einstieg**")
                    t = worst_diag.get("breakout_breakdown")
                    if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
            with diag_tabs[4]:
                c_left, c_right = st.columns(2)
                with c_left:
                    st.markdown("**Haltedauer**")
                    t = worst_diag.get("holding_breakdown")
                    if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
                with c_right:
                    st.markdown("**Wochentag des Einstiegs**")
                    t = worst_diag.get("weekday_breakdown")
                    if isinstance(t, pd.DataFrame) and not t.empty: st.dataframe(t, use_container_width=True, hide_index=True)
            with diag_tabs[5]:
                t = worst_diag.get("sells")
                if isinstance(t, pd.DataFrame) and not t.empty:
                    show = t.sort_values("timestamp", ascending=False)
                    st.dataframe(show, use_container_width=True, hide_index=True)
                    st.download_button("⬇ Trades des schlechtesten Blocks als CSV", data=show.to_csv(index=False).encode("utf-8-sig"), file_name="v4_9_worst_block_trades.csv", mime="text/csv")
                else:
                    st.info("Keine abgeschlossenen Trades im schlechtesten Block.")
            st.info("V4.8 nimmt aus dieser Diagnose **keine automatische Regeländerung** vor. Erst wenn eine plausible Änderung anschließend wieder in getrennten Zeitblöcken besser abschneidet, wäre sie ein Kandidat für V4.9.")

        holdout_result = st.session_state.get("breakout_holdout_result")
        if holdout_result is not None:
            st.markdown("#### Ungesehener Holdout – Kontoverlauf des Rang-1-Kandidaten")
            if isinstance(holdout_result.equity, pd.Series) and not holdout_result.equity.empty:
                st.line_chart(holdout_result.equity)
            if isinstance(holdout_result.trade_log, pd.DataFrame) and not holdout_result.trade_log.empty:
                st.markdown("#### Holdout-Trades")
                st.dataframe(holdout_result.trade_log.iloc[::-1], use_container_width=True, hide_index=True)
            audit = breakout_audit(holdout_result, float(start_capital))
            st.markdown("#### Kontrollrechnung Holdout")
            a1,a2,a3,a4,a5 = st.columns(5)
            a1.metric("Start", f"{audit['start_capital']:,.2f} €")
            a2.metric("Brutto Trade-P/L", f"{audit['gross_pnl_before_fees']:+,.2f} €")
            a3.metric("Gebühren", f"-{audit['fees']:,.2f} €")
            a4.metric("Endwert", f"{audit['final_value']:,.2f} €")
            a5.metric("Prüfdifferenz", f"{audit['difference']:+.4f} €")
            st.caption("Kontrolle: Start + Brutto-Trade-P/L − Gebühren = Endwert. Die Prüfdifferenz sollte praktisch 0 € sein.")
        st.warning("Auch ein ROBUST-KANDIDAT ist nur ein historischer Filter. Reale Slippage, Spreads, Datenabweichungen und Marktänderungen können das Ergebnis verschlechtern. V5 bleibt Paper-Trading.")
    else:
        st.info("Empfohlener erster Lauf: BTC, ETH, SOL, LINK, AVAX · 365 Tage · 6 Stunden · 250 Tests. Für eine zweite Runde kannst du 500 Tests nehmen.")


with hypothesis_tab:
    st.subheader("Gezielter Hypothesen-Test – eine Änderung nach der anderen")
    st.caption("Der Hypothesen-Tab aus V4.9 testet **keine hunderten Zufallskombinationen**. Die V4.8-Diagnose wird in sechs vorher festgelegte Varianten übersetzt: Referenz, RSI ≤72, Breakout ≥85, zwei Bestätigungen, RSI+Breakout kombiniert und Volumen ≥2×.")
    st.info("Wichtig: Die Rangfolge entsteht ausschließlich aus vier älteren Entwicklungsblöcken. Der neueste Abschnitt wird erst danach berechnet. Da frühere Versionen zeitlich überlappende Daten bereits gezeigt haben, nennt der Hypothesen-Test ihn bewusst **historischen Bestätigungsabschnitt** und nicht 'neuen unangetasteten Final-Holdout'. Für wirklich neue Daten gibt es darunter den Forward-Shadow-Test.")

    h1,h2,h3 = st.columns([2.2,1,1])
    with h1:
        hypo_assets = st.multiselect("Coins Hypothesentest", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="hypo_assets")
    with h2:
        hypo_days = st.selectbox("Zeitraum", [180,365,540], index=1, format_func=lambda x:f"letzte {x} Tage", key="hypo_days")
    with h3:
        hypo_scan_hours = st.selectbox("Historisch scannen alle", [3,6,12], index=1, format_func=lambda x:f"{x} Std.", key="hypo_scan_hours")

    st.markdown("**Fest vor dem Lauf definierte Hypothesen:** Referenz · RSI ≤72 · Breakout ≥85 · 2 Bestätigungen · RSI ≤72 + Breakout ≥85 · Volumen ≥2×")
    if st.button("🧬 Hypothesen vergleichen", type="primary"):
        if len(hypo_assets) < 2 or "BTC" not in hypo_assets:
            st.error("Bitte mindestens zwei Coins wählen und BTC als Marktphasen-Benchmark enthalten.")
        else:
            end_dt = datetime.now(); start_dt = end_dt - timedelta(days=int(hypo_days)+40)
            data = {}; failures=[]
            progress = st.progress(0, text="Lade 1h-Kursdaten …")
            for i, asset in enumerate(hypo_assets):
                try:
                    data[asset] = cached_hourly(CRYPTO_UNIVERSE[asset], start_dt.isoformat(), end_dt.isoformat())
                except Exception as exc:
                    failures.append(f"{asset}: {exc}")
                progress.progress((i+1)/len(hypo_assets), text=f"Kursdaten {i+1}/{len(hypo_assets)}")
            progress.empty()
            if failures:
                st.warning("Einzelne Datenquellen fehlgeschlagen: " + " | ".join(failures[:3]))
            try:
                prep = st.progress(0, text="Historische Merkmale vorbereiten …")
                snapshots = prepare_snapshots(
                    data, test_days=int(hypo_days), scan_every_hours=int(hypo_scan_hours),
                    progress_cb=lambda x: prep.progress(float(x), text=f"Historie vorbereiten: {int(x*100)} %")
                )
                prep.empty()
                runp = st.progress(0, text="Sechs Hypothesen über vier Entwicklungsblöcke prüfen …")
                dev_df, confirm_df, hypo_summary, hypo_extra = run_hypothesis_test(
                    snapshots, start_capital=float(start_capital), allocation_pct=float(allocation), fee_pct=float(fee),
                    max_positions=int(max_positions), daily_loss_limit_pct=float(daily_loss), n_dev_blocks=4,
                    confirmation_fraction=0.20,
                    progress_cb=lambda x: runp.progress(float(x), text=f"Hypothesentest: {int(x*100)} %")
                )
                runp.empty()
                st.session_state["hypo_dev_df"] = dev_df
                st.session_state["hypo_confirm_df"] = confirm_df
                st.session_state["hypo_summary"] = hypo_summary
                st.session_state["hypo_extra"] = hypo_extra
                st.session_state["hypo_assets_used"] = list(hypo_assets)
                st.session_state["hypo_scan_hours_used"] = int(hypo_scan_hours)
                st.success("Hypothesentest abgeschlossen. Die historische Bestätigung wurde erst nach dem Entwicklungs-Ranking berechnet.")
            except Exception as exc:
                st.error(f"Hypothesentest fehlgeschlagen: {exc}")

    hypo_dev = st.session_state.get("hypo_dev_df")
    hypo_confirm = st.session_state.get("hypo_confirm_df")
    hs = st.session_state.get("hypo_summary")
    hx = st.session_state.get("hypo_extra")
    if isinstance(hypo_dev, pd.DataFrame) and not hypo_dev.empty and isinstance(hs, dict):
        overall = hs.get("overall", "SCHWACH")
        if overall == "PAPER-KANDIDAT":
            st.success("🟢 **PAPER-KANDIDAT** – die fest definierte Variante war in der Entwicklung stark und blieb im historischen Bestätigungsabschnitt positiv. Weiterhin kein Live-Freibrief.")
        elif overall == "WEITER TESTEN":
            st.warning("🟡 **WEITER TESTEN** – die Variante verbessert die Stabilität, erfüllt aber noch nicht alle strengen Kriterien.")
        else:
            st.error("🔴 **NOCH NICHT STABIL** – keine der sechs gezielten Änderungen löst das Problem ausreichend.")

        m1,m2,m3,m4,m5,m6 = st.columns(6)
        m1.metric("Gewinner Entwicklung", str(hs.get("winner","–")))
        m2.metric("Positive Blöcke", str(hs.get("positive_blocks","–")))
        m3.metric("Entwicklung", f"{hs.get('dev_combined_pct',0):+.2f} %")
        m4.metric("Schlechtester Block", f"{hs.get('dev_worst_pct',0):+.2f} %")
        m5.metric("Ø PF", f"{hs.get('dev_pf',0):.2f}")
        m6.metric("Hist. Bestätigung", f"{hs.get('confirmation_pct',0):+.2f} %", f"PF {hs.get('confirmation_pf',0):.2f}")
        st.write(f"**Getestete Gewinner-Hypothese:** {hs.get('winner_thesis','–')}")

        st.markdown("#### 1) Vier Entwicklungsblöcke – **darauf wird entschieden**")
        st.dataframe(hypo_dev, use_container_width=True, hide_index=True)
        st.download_button("⬇ Hypothesen-Entwicklung als CSV", data=hypo_dev.to_csv(index=False).encode("utf-8-sig"), file_name="v4_9_hypothesen_entwicklung.csv", mime="text/csv")

        if isinstance(hx, dict) and isinstance(hx.get("rescue"), pd.DataFrame):
            st.markdown(f"#### 2) Rettet eine Hypothese den bisherigen Problemblock #{hs.get('reference_worst_block','–')}?")
            st.caption("Hier wird für alle Varianten exakt derselbe Block betrachtet, der bei der Referenz am schlechtesten war. Das verhindert, dass sich jede Variante ihren eigenen 'schönen' Vergleich aussucht.")
            st.dataframe(hx["rescue"], use_container_width=True, hide_index=True)

        if isinstance(hypo_confirm, pd.DataFrame) and not hypo_confirm.empty:
            st.markdown("#### 3) Historischer Bestätigungsabschnitt – **nicht zum Ranking benutzt**")
            st.dataframe(hypo_confirm, use_container_width=True, hide_index=True)
            st.warning(str(hs.get("note", "Dieser Abschnitt kann mit Daten überlappen, die in früheren Versionen bereits betrachtet wurden.")))

        st.markdown("### ⏱️ Echter Forward-Shadow-Test mit zukünftigen Daten")
        st.caption("Ein wirklich neuer Holdout kann nicht rückwirkend erzeugt werden. Der Hypothesen-Test kann deshalb den Entwicklungs-Gewinner **ab jetzt einfrieren**. Spätere Kurse nach diesem Zeitpunkt sind dann tatsächlich neue Daten; die Regeln werden während des Shadow-Tests nicht nachoptimiert.")
        assets_used = st.session_state.get("hypo_assets_used", ["BTC","ETH","SOL","LINK","AVAX"])
        scan_used = int(st.session_state.get("hypo_scan_hours_used", 6))
        if st.button("⏱️ Gewinner ab jetzt für Forward-Shadow einfrieren"):
            payload = {
                "version": "V4.9", "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "variant": hs.get("winner"), "thesis": hs.get("winner_thesis"), "params": hs.get("winner_params", {}),
                "assets": list(assets_used), "scan_hours": scan_used,
                "start_capital": float(start_capital), "allocation_pct": float(allocation), "fee_pct": float(fee),
                "max_positions": int(max_positions), "daily_loss_limit_pct": float(daily_loss),
            }
            write_json(SHADOW_FILE, payload)
            st.success("Forward-Shadow eingefroren. Ab jetzt zählt nur Kursmaterial nach diesem Zeitstempel.")

    shadow = read_json(SHADOW_FILE, {})
    if isinstance(shadow, dict) and shadow.get("started_at"):
        st.markdown("#### Laufender Forward-Shadow")
        try:
            shadow_start = datetime.fromisoformat(str(shadow["started_at"]))
            elapsed_h = max(0.0, (datetime.now().astimezone() - shadow_start).total_seconds()/3600.0)
        except Exception:
            shadow_start = datetime.now().astimezone(); elapsed_h = 0.0
        s1,s2,s3 = st.columns(3)
        s1.metric("Eingefrorene Variante", str(shadow.get("variant","–")))
        s2.metric("Start", shadow_start.strftime("%d.%m.%Y %H:%M"))
        s3.metric("Neue Daten", f"{elapsed_h/24:.1f} Tage")
        st.caption("Für einen sinnvollen 6h-Shadow-Test sollten mindestens einige Tage und später deutlich mehr Trades vergehen. Währenddessen die eingefrorenen Regeln nicht anhand des Ergebnisses verändern.")
        if st.button("🔍 Forward-Shadow jetzt auswerten"):
            if elapsed_h < max(48.0, float(shadow.get("scan_hours",6))*8):
                st.warning("Noch zu wenig wirklich neue Zeit vergangen. Warte mindestens etwa 2–3 Tage; für eine Aussage über die Strategie eher Wochen und viele Trades.")
            else:
                try:
                    now_dt = datetime.now().astimezone()
                    warmup_start = shadow_start - timedelta(days=40)
                    sdata = {}
                    shadow_assets = list(shadow.get("assets") or ["BTC","ETH","SOL","LINK","AVAX"])
                    lp = st.progress(0, text="Neue Shadow-Kursdaten laden …")
                    for i, asset in enumerate(shadow_assets):
                        sdata[asset] = cached_hourly(CRYPTO_UNIVERSE[asset], warmup_start.isoformat(), now_dt.isoformat())
                        lp.progress((i+1)/len(shadow_assets), text=f"Shadow-Daten {i+1}/{len(shadow_assets)}")
                    lp.empty()
                    elapsed_days = max(3, int(math.ceil(elapsed_h/24.0))+1)
                    sp = prepare_snapshots(sdata, test_days=elapsed_days, scan_every_hours=int(shadow.get("scan_hours",6)))
                    sp = [x for x in sp if pd.Timestamp(x["timestamp"]) >= pd.Timestamp(shadow_start)]
                    if len(sp) < 8:
                        st.warning("Noch zu wenige neue Scanner-Zeitpunkte für eine sinnvolle Auswertung.")
                    else:
                        sr = simulate_breakout(
                            sp, params=dict(shadow.get("params") or {}), start_capital=float(shadow.get("start_capital",1000.0)),
                            allocation_pct=float(shadow.get("allocation_pct",20.0)), fee_pct=float(shadow.get("fee_pct",0.25)),
                            max_positions=int(shadow.get("max_positions",2)), daily_loss_limit_pct=float(shadow.get("daily_loss_limit_pct",3.0)),
                            collect_details=True,
                        )
                        z1,z2,z3,z4 = st.columns(4)
                        z1.metric("Forward-Rendite", f"{sr.return_pct:+.2f} %")
                        z2.metric("Forward-DD", f"-{sr.max_drawdown_pct:.2f} %")
                        z3.metric("Forward-PF", "∞" if math.isinf(sr.profit_factor) else f"{sr.profit_factor:.2f}")
                        z4.metric("Abgeschl. Trades", str(sr.completed_trades))
                        if isinstance(sr.equity, pd.Series) and not sr.equity.empty:
                            st.line_chart(sr.equity)
                        if isinstance(sr.trade_log, pd.DataFrame) and not sr.trade_log.empty:
                            st.dataframe(sr.trade_log.iloc[::-1], use_container_width=True, hide_index=True)
                        st.info("Das ist der methodisch wichtigste Test dieses Hypothesen-Moduls: Diese Daten lagen beim Einfrieren der Regeln noch nicht vor. Auch hier sind wenige Tage/Trades noch keine belastbare Evidenz.")
                except Exception as exc:
                    st.error(f"Forward-Shadow konnte noch nicht ausgewertet werden: {exc}")



with rotation_tab:
    st.subheader("Relative-Stärke / Rotation – nur die aktuell stärksten Coins halten")
    st.caption("V5 testet eine bewusst andere Idee als Breakout: Coins werden relativ zueinander gerankt. Gehalten werden nur die Top-Kandidaten; bei schwachem BTC-Gesamtmarkt kann der Bot komplett in Cash wechseln. Die sechs Varianten sind vor dem Lauf fest definiert – kein Parameter-Grid.")
    rc1,rc2,rc3=st.columns([1.5,1,1])
    with rc1:
        rot_assets=st.multiselect("Coins Rotation", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="rot_assets")
    with rc2:
        rot_days=st.selectbox("Zeitraum Rotation", [180,365,540], index=1, format_func=lambda x:f"letzte {x} Tage", key="rot_days")
    with rc3:
        rot_scan=st.selectbox("Historisch scannen alle", [6,12,24], index=0, format_func=lambda x:f"{x} Std.", key="rot_scan")
    st.info("Score = relative 7T-/30T-Stärke + Trend + etwas Volatilitätskontrolle. Verglichen werden Top-1/Top-2/Top-3, bull-only/no-bear, strengere Mindeststärke und 12h/24h-Rotation. Gebühren werden bei jedem echten Wechsel berücksichtigt.")
    if st.button("🔄 Rotation vergleichen", type="primary"):
        if len(rot_assets)<2:
            st.error("Bitte mindestens zwei Coins auswählen.")
        else:
            try:
                now_dt=datetime.now().astimezone(); start_dt=now_dt-timedelta(days=int(rot_days)+70)
                rp=st.progress(0,text="Historische Rotationsdaten laden …")
                rdata={}
                for i,a in enumerate(rot_assets):
                    rdata[a]=cached_hourly(CRYPTO_UNIVERSE[a],start_dt.isoformat(),now_dt.isoformat())
                    rp.progress((i+1)/len(rot_assets)*0.35,text=f"Rotation-Daten {i+1}/{len(rot_assets)}")
                snaps=prepare_rotation_snapshots(rdata,test_days=int(rot_days),scan_every_hours=int(rot_scan))
                rp.progress(0.4,text="Vier Entwicklungsblöcke + Bestätigung rechnen …")
                dev_df, conf_df, rs, rd=compare_rotation_strategies(snaps,start_capital=float(start_capital),fee_pct=float(fee),progress_cb=lambda x:rp.progress(0.4+float(x)*0.6,text=f"Rotation: {int(x*100)} %"))
                rp.empty()
                st.session_state["rotation_dev_df"]=dev_df; st.session_state["rotation_confirm_df"]=conf_df; st.session_state["rotation_summary"]=rs; st.session_state["rotation_detail"]=rd
                st.session_state["rotation_assets_used"]=list(rot_assets); st.session_state["rotation_scan_used"]=int(rot_scan)
                st.session_state["rotation_snapshots"]=snaps
                st.success("Rotationsvergleich abgeschlossen. Die historische Bestätigung wurde erst nach dem Entwicklungs-Ranking berechnet.")
            except Exception as exc:
                st.error(f"Rotationstest fehlgeschlagen: {exc}")
    rdev=st.session_state.get("rotation_dev_df"); rconf=st.session_state.get("rotation_confirm_df"); rs=st.session_state.get("rotation_summary"); rd=st.session_state.get("rotation_detail")
    if isinstance(rdev,pd.DataFrame) and not rdev.empty and isinstance(rs,dict):
        if rs.get("overall")=="PAPER-KANDIDAT": st.success("🟢 **PAPER-KANDIDAT** – Rotation war in der Entwicklung stabil und blieb in der historischen Bestätigung positiv. Noch kein Live-Freibrief.")
        elif rs.get("overall")=="WEITER TESTEN": st.warning("🟡 **WEITER TESTEN** – relative Stärke sieht besser aus als einige frühere Ansätze, erfüllt aber noch nicht alle Robustheitskriterien.")
        else: st.error("🔴 **NOCH NICHT STABIL** – auch Rotation liefert bisher keinen belastbaren Vorteil.")
        q1,q2,q3,q4,q5,q6=st.columns(6)
        q1.metric("Gewinner",str(rs.get("winner","–"))); q2.metric("Positive Blöcke",str(rs.get("positive_blocks","–")))
        q3.metric("Entwicklung",f"{rs.get('dev_return',0):+.2f} %"); q4.metric("Schlechtester Block",f"{rs.get('worst',0):+.2f} %")
        q5.metric("Ø PF",f"{rs.get('pf',0):.2f}"); q6.metric("Hist. Bestätigung",f"{rs.get('confirm_return',0):+.2f} %",f"PF {rs.get('confirm_pf',0):.2f}")
        st.markdown("#### 1) Vier Entwicklungsblöcke – darauf wird entschieden")
        st.dataframe(rdev,use_container_width=True,hide_index=True)
        if isinstance(rconf,pd.DataFrame) and not rconf.empty:
            st.markdown("#### 2) Historischer Bestätigungsabschnitt – nicht zum Ranking benutzt")
            st.dataframe(rconf,use_container_width=True,hide_index=True)
        snaps=st.session_state.get("rotation_snapshots")
        if isinstance(snaps,list) and snaps:
            btc_bh=benchmark_rotation_buy_hold(snaps,"BTC",float(start_capital),float(fee))
            b1,b2=st.columns(2); b1.metric("Benchmark BTC Buy & Hold",f"{btc_bh['return_pct']:+.2f} %"); b2.metric("Gewinner Bestätigung DD",f"-{rs.get('confirm_dd',0):.2f} %")
        if isinstance(rd,dict) and rs.get("winner") in rd:
            cr=rd[rs["winner"]].get("confirmation")
            if cr is not None and isinstance(cr.equity,pd.Series) and not cr.equity.empty:
                st.markdown("#### Kontoverlauf des Gewinners im Bestätigungsabschnitt")
                st.line_chart(cr.equity)
            if cr is not None and isinstance(cr.trade_log,pd.DataFrame) and not cr.trade_log.empty:
                with st.expander("Rotation-Trades im Bestätigungsabschnitt"):
                    st.dataframe(cr.trade_log.iloc[::-1],use_container_width=True,hide_index=True)
        st.markdown("### ⏱️ Rotation als echten Forward-Shadow einfrieren")
        st.caption("Wenn du den Entwicklungs-Gewinner einfrierst, werden seine Regeln nicht mehr verändert. Erst danach eintreffende Kurse sind wirklich neue Daten.")
        if st.button("⏱️ Rotations-Gewinner ab jetzt einfrieren"):
            payload={"version":"V5","started_at":datetime.now().astimezone().isoformat(timespec="seconds"),"variant":rs.get("winner"),"params":rs.get("winner_params",{}),"assets":st.session_state.get("rotation_assets_used",["BTC","ETH","SOL","LINK","AVAX"]),"scan_hours":int(st.session_state.get("rotation_scan_used",6)),"start_capital":float(start_capital),"fee_pct":float(fee)}
            write_json(ROTATION_SHADOW_FILE,payload); st.success("Rotations-Forward-Shadow eingefroren. Ab jetzt die Regeln nicht anhand des Ergebnisses ändern.")
    rshadow=read_json(ROTATION_SHADOW_FILE,{})
    if isinstance(rshadow,dict) and rshadow.get("started_at"):
        st.markdown("#### Laufender Rotation-Forward-Shadow")
        try:
            rstart=datetime.fromisoformat(str(rshadow["started_at"])); elapsed=max(0.0,(datetime.now().astimezone()-rstart).total_seconds()/3600)
        except Exception:
            rstart=datetime.now().astimezone(); elapsed=0.0
        z1,z2,z3=st.columns(3); z1.metric("Variante",str(rshadow.get("variant","–"))); z2.metric("Start",rstart.strftime("%d.%m.%Y %H:%M")); z3.metric("Neue Daten",f"{elapsed/24:.1f} Tage")
        if st.button("🔍 Rotation-Shadow jetzt auswerten"):
            if elapsed<72:
                st.warning("Noch zu wenig wirklich neue Zeit. Für eine erste technische Prüfung mindestens etwa 3 Tage, für Aussagekraft eher Wochen und genügend Umschichtungen abwarten.")
            else:
                try:
                    now_dt=datetime.now().astimezone(); warm=rstart-timedelta(days=70); d={}
                    for a in list(rshadow.get("assets") or []): d[a]=cached_hourly(CRYPTO_UNIVERSE[a],warm.isoformat(),now_dt.isoformat())
                    snaps=prepare_rotation_snapshots(d,test_days=max(4,int(math.ceil(elapsed/24))+1),scan_every_hours=int(rshadow.get("scan_hours",6)))
                    cutoff=pd.Timestamp(rstart)
                    if cutoff.tzinfo is not None:
                        cutoff=cutoff.tz_localize(None)
                    snaps=[x for x in snaps if pd.Timestamp(x["timestamp"]).tz_localize(None) >= cutoff if pd.Timestamp(x["timestamp"]).tzinfo is not None] or [x for x in snaps if pd.Timestamp(x["timestamp"]) >= cutoff]
                    if len(snaps)<4: st.warning("Noch zu wenige neue Rotation-Zeitpunkte.")
                    else:
                        rr=simulate_rotation(snaps,params=dict(rshadow.get("params") or {}),start_capital=float(rshadow.get("start_capital",1000)),fee_pct=float(rshadow.get("fee_pct",0.25)))
                        y1,y2,y3,y4=st.columns(4); y1.metric("Forward-Rendite",f"{rr.return_pct:+.2f} %"); y2.metric("Forward-DD",f"-{rr.max_drawdown_pct:.2f} %"); y3.metric("Forward-PF","∞" if math.isinf(rr.profit_factor) else f"{rr.profit_factor:.2f}"); y4.metric("Verkäufe",str(rr.completed_trades))
                        st.line_chart(rr.equity)
                        if not rr.trade_log.empty: st.dataframe(rr.trade_log.iloc[::-1],use_container_width=True,hide_index=True)
                except Exception as exc: st.error(f"Rotation-Shadow konnte noch nicht ausgewertet werden: {exc}")


with defensive_tab:
    st.subheader("Defensiver Kern – BTC / ETH / Cash")
    st.caption("Absichtlich einfach: maximal eine Position. Nur wenn ein sauberer Trend vorliegt, wird BTC oder ETH gehalten; sonst bleibt das Kapital in Cash. Keine Stop-Loss-Optimierung, kein Altcoin-Ranking.")
    d1,d2=st.columns([1,1])
    with d1:
        def_days=st.selectbox("Zeitraum defensiv", [180,365,540,730], index=1, format_func=lambda x:f"letzte {x} Tage", key="def_days")
    with d2:
        def_scan=st.selectbox("Historisch prüfen alle", [3,6,12,24], index=1, format_func=lambda x:f"{x} Std.", key="def_scan")
    st.info("Verglichen werden nur sechs vorab definierte Regeln. Kerngedanke: Preis über langfristigem Trend + positiver Trend, optional positives 7T/30T-Momentum. Bei keinem gültigen Setup = Cash.")
    if st.button("🛡️ Defensiv-System vergleichen", type="primary"):
        try:
            end_dt=datetime.now(); start_dt=end_dt-timedelta(days=int(def_days)+90); dd={}
            pr=st.progress(0,text="BTC/ETH-Daten laden …")
            for i,a in enumerate(["BTC","ETH"]):
                dd[a]=cached_hourly(CRYPTO_UNIVERSE[a], start_dt.isoformat(), end_dt.isoformat())
                pr.progress((i+1)/2*0.35,text=f"{a}-Daten geladen")
            snaps=prepare_defensive_snapshots(dd,test_days=int(def_days),scan_every_hours=int(def_scan))
            dev_df,conf_df,ds,detail=compare_defensive_strategies(snaps,start_capital=float(start_capital),fee_pct=float(fee),progress_cb=lambda x:pr.progress(0.35+float(x)*0.65,text=f"Defensiv-Test: {int(x*100)} %"))
            pr.empty(); st.session_state["defensive_result"]=(dev_df,conf_df,ds,detail,snaps)
            st.success("Defensiv-Vergleich abgeschlossen. Die historische Bestätigung wurde erst nach dem Entwicklungs-Ranking berechnet.")
        except Exception as exc:
            st.error(f"Defensiv-Test fehlgeschlagen: {exc}")
    dr=st.session_state.get("defensive_result")
    if dr:
        dev_df,conf_df,ds,detail,snaps=dr
        if ds.get("overall")=="PAPER-KANDIDAT":
            st.success("🟢 **PAPER-KANDIDAT** – das einfache Defensiv-System war in der Entwicklung stabil und blieb in der historischen Bestätigung positiv. Noch kein Live-Freibrief.")
        elif ds.get("overall")=="WEITER TESTEN":
            st.warning("🟡 **WEITER TESTEN** – deutlich interessanter als instabile Varianten, aber noch nicht robust genug für echtes Geld.")
        else:
            st.error("🔴 **NOCH NICHT STABIL** – auch die einfache BTC/ETH/Cash-Logik liefert bisher keinen belastbaren Vorteil.")
        c1,c2,c3,c4,c5,c6=st.columns(6)
        c1.metric("Gewinner",str(ds.get("winner","–")))
        c2.metric("Positive Blöcke",str(ds.get("positive_blocks","–")))
        c3.metric("Entwicklung",f"{float(ds.get('dev_return',0)):+.2f} %")
        c4.metric("Schlechtester Block",f"{float(ds.get('worst',0)):+.2f} %")
        c5.metric("Ø PF",f"{float(ds.get('pf',0)):.2f}")
        c6.metric("Hist. Bestätigung",f"{float(ds.get('confirm_return',0)):+.2f} %")
        st.markdown("### 1) Vier Entwicklungsblöcke")
        st.dataframe(dev_df,use_container_width=True,hide_index=True)
        st.markdown("### 2) Historischer Bestätigungsabschnitt")
        st.dataframe(conf_df,use_container_width=True,hide_index=True)
        btc_b=defensive_buy_hold(snaps,"BTC",start_capital=float(start_capital),fee_pct=float(fee))
        eth_b=defensive_buy_hold(snaps,"ETH",start_capital=float(start_capital),fee_pct=float(fee))
        b1,b2,b3=st.columns(3)
        b1.metric("BTC Buy & Hold",f"{btc_b['return_pct']:+.2f} %")
        b2.metric("ETH Buy & Hold",f"{eth_b['return_pct']:+.2f} %")
        b3.metric("Cash-Anteil Gewinner (Bestätigung)",f"{float(ds.get('confirm_cash_pct',0)):.1f} %")
        winner=str(ds.get("winner")); wr=detail[winner]["confirmation"]
        st.markdown("### Kontoverlauf des Gewinners im Bestätigungsabschnitt")
        st.line_chart(wr.equity)
        if not wr.trade_log.empty:
            with st.expander("Trades des Defensiv-Gewinners"):
                st.dataframe(wr.trade_log.iloc[::-1],use_container_width=True,hide_index=True)
        st.caption("Wichtig: Das System darf viel Zeit in Cash verbringen. Weniger Marktzeit ist hier Absicht, nicht automatisch ein Fehler.")


with forward_tab:
    st.subheader("🏁 Eingefrorener Forward-Paper-Wettkampf")
    st.caption("Ab dem Startzeitpunkt werden die Regeln nicht mehr an Backtests angepasst. Alle drei Strategien beginnen unabhängig voneinander mit demselben virtuellen Kapital. Nur danach eintreffende Kurse zählen.")

    comp = read_json(FORWARD_COMP_FILE, {})
    if not comp:
        f1,f2,f3=st.columns([1.4,1,1])
        with f1:
            fw_assets=st.multiselect("Quality-Breakout-Universum", list(CRYPTO_UNIVERSE.keys()), default=["BTC","ETH","SOL","LINK","AVAX"], key="fw_assets")
        with f2:
            fw_scan=st.selectbox("Prüfrhythmus", [3,6,12], index=1, format_func=lambda x:f"{x} Std.", key="fw_scan")
        with f3:
            fw_cap=st.number_input("Startkapital je Strategie (€)", 100.0, 100000.0, 1000.0, 100.0, key="fw_cap")
        st.info("Eingefrorene Teilnehmer: **A Quality Breakout** (feste V4.7-Regeln), **B BTC EMA 50/200**, **C BTC Buy & Hold**. Die Gebühren werden mit deinem aktuellen Simulationswert übernommen.")
        with st.expander("Eingefrorene Quality-Breakout-Regeln anzeigen"):
            st.json(quality_breakout_frozen_params())
        if st.button("🏁 Wettkampf ab jetzt starten", type="primary"):
            if "BTC" not in fw_assets or len(fw_assets)<2:
                st.error("Bitte BTC und mindestens einen weiteren Coin für Quality Breakout auswählen.")
            else:
                payload={
                    "version":"5.2", "started_at":datetime.now().astimezone().isoformat(timespec="seconds"),
                    "start_capital":float(fw_cap), "fee_pct":float(fee), "scan_hours":int(fw_scan),
                    "assets":list(fw_assets), "quality_params":quality_breakout_frozen_params(),
                    "participants":["Quality Breakout","BTC EMA 50/200","BTC Buy & Hold"],
                    "rules_locked":True,
                }
                write_json(FORWARD_COMP_FILE,payload)
                if FORWARD_HISTORY_FILE.exists(): FORWARD_HISTORY_FILE.unlink()
                st.success("Wettkampf eingefroren. Ab diesem Zeitstempel zählen nur neue Kurse. Seite einmal neu laden oder unten direkt auswerten.")
                comp=payload
    if comp:
        start_ts=pd.Timestamp(comp["started_at"])
        now_ts=pd.Timestamp(datetime.now().astimezone())
        elapsed_h=max(0.0,(now_ts-start_ts).total_seconds()/3600.0)
        elapsed_days=elapsed_h/24.0
        h1,h2,h3,h4=st.columns(4)
        h1.metric("Start",str(start_ts.strftime("%d.%m.%Y %H:%M")))
        h2.metric("Laufzeit",f"{elapsed_days:.1f} Tage")
        h3.metric("Startkapital je Bot",f"{float(comp.get('start_capital',1000)):.0f} €")
        h4.metric("Regeln", "🔒 eingefroren")
        st.progress(min(1.0,elapsed_days/30.0),text=f"30-Tage-Mindesttest: {min(elapsed_days,30):.1f}/30 Tage")
        if elapsed_days < 30:
            st.warning("Das ist noch nur ein Zwischenstand. Vor 30 Tagen würde ich daraus keine Strategieentscheidung ableiten; 60–90 Tage sind besser.")
        elif elapsed_days < 60:
            st.info("30 Tage sind erreicht. Für mehr Aussagekraft weiter bis mindestens 60 Tage laufen lassen.")
        else:
            st.success("Mindestens 60 Tage Forward-Daten erreicht. Jetzt werden die Ergebnisse deutlich interessanter – weiterhin kein Gewinnversprechen.")

        st.markdown("#### Fest eingefrorene Regeln")
        st.write(f"Quality Breakout: {', '.join(comp.get('assets', []))} · alle {int(comp.get('scan_hours',6))}h · 20 % je Position · max. 2 Positionen · feste Quality-Regeln. BTC EMA 50/200 und BTC Buy & Hold handeln nur BTC.")
        with st.expander("Quality-Regeln"):
            st.json(comp.get("quality_params",{}))

        if st.button("🔍 Forward-Wettkampf jetzt auswerten", type="primary"):
            try:
                now_dt=datetime.now().astimezone(); start_dt=pd.Timestamp(comp["started_at"]).to_pydatetime()
                warmup_start=start_dt-timedelta(days=90)
                assets=list(dict.fromkeys(list(comp.get("assets") or ["BTC","ETH","SOL","LINK","AVAX"])+["BTC"]))
                lp=st.progress(0,text="Nur Forward-Kurse + Warmup laden …")
                fdata={}
                for i,a in enumerate(assets):
                    fdata[a]=cached_hourly(CRYPTO_UNIVERSE[a],warmup_start.isoformat(),now_dt.isoformat())
                    lp.progress((i+1)/len(assets)*0.55,text=f"Forward-Daten {i+1}/{len(assets)}")
                table,results,eq=evaluate_forward_competition(
                    fdata,started_at=comp["started_at"],scan_hours=int(comp.get("scan_hours",6)),
                    start_capital=float(comp.get("start_capital",1000)),fee_pct=float(comp.get("fee_pct",0.25)),
                    quality_params=dict(comp.get("quality_params") or quality_breakout_frozen_params()),
                )
                lp.progress(1.0,text="Wettkampf ausgewertet"); lp.empty()
                st.session_state["forward_table"]=table; st.session_state["forward_results"]=results; st.session_state["forward_equity"]=eq
                st.session_state["forward_eval_at"]=datetime.now().astimezone().isoformat(timespec="seconds")

                hist_rows=[]
                for _,row in table.iterrows():
                    if pd.notna(row.get("Kontowert €")):
                        hist_rows.append({"timestamp":st.session_state["forward_eval_at"],"strategy":row["Strategie"],"value_eur":row["Kontowert €"],"return_pct":row["Rendite %"],"drawdown_pct":row["Drawdown %"],"trades":row["Abgeschl. Trades"]})
                if hist_rows:
                    hdf=pd.DataFrame(hist_rows)
                    if FORWARD_HISTORY_FILE.exists():
                        oldh=pd.read_csv(FORWARD_HISTORY_FILE)
                        allh=pd.concat([oldh,hdf],ignore_index=True)
                        allh=allh.drop_duplicates(subset=["timestamp","strategy"],keep="last")
                    else: allh=hdf
                    allh.to_csv(FORWARD_HISTORY_FILE,index=False)
                st.success("Forward-Zwischenstand aktualisiert.")
            except Exception as exc:
                st.error(f"Forward-Wettkampf konnte noch nicht ausgewertet werden: {exc}")

        ft=st.session_state.get("forward_table"); fr=st.session_state.get("forward_results"); feq=st.session_state.get("forward_equity")
        if isinstance(ft,pd.DataFrame) and not ft.empty:
            st.markdown("### Aktueller Zwischenstand")
            available=ft[pd.to_numeric(ft["Kontowert €"],errors="coerce").notna()].sort_values("Kontowert €",ascending=False)
            if not available.empty:
                leader=available.iloc[0]
                a,b,c,d=st.columns(4)
                a.metric("Aktuell vorne",str(leader["Strategie"]))
                b.metric("Kontowert",f"{float(leader['Kontowert €']):,.2f} €")
                c.metric("Rendite",f"{float(leader['Rendite %']):+.2f} %")
                d.metric("Vorsprung",f"{float(leader['Kontowert €'])-float(available.iloc[1]['Kontowert €']):+.2f} €" if len(available)>1 else "–")
            st.dataframe(ft,use_container_width=True,hide_index=True)
            if isinstance(feq,pd.DataFrame) and not feq.empty:
                st.markdown("### Kontoverlauf seit dem Einfrieren")
                st.line_chart(feq)
            if isinstance(fr,dict):
                tabs=st.tabs(["Quality Breakout","BTC EMA 50/200","BTC Buy & Hold"])
                for tab,name in zip(tabs,["Quality Breakout","BTC EMA 50/200","BTC Buy & Hold"]):
                    with tab:
                        r=fr.get(name)
                        if r is None: st.info("Für diese Strategie liegen noch nicht genug neue Daten vor.")
                        else:
                            st.caption(r.note)
                            if isinstance(r.trade_log,pd.DataFrame) and not r.trade_log.empty:
                                st.dataframe(r.trade_log.iloc[::-1],use_container_width=True,hide_index=True)
                            else: st.info("Noch keine Signal-Trades.")
        if FORWARD_HISTORY_FILE.exists():
            try:
                hist=pd.read_csv(FORWARD_HISTORY_FILE)
                if not hist.empty:
                    pivot=hist.pivot_table(index="timestamp",columns="strategy",values="value_eur",aggfunc="last")
                    if len(pivot)>1:
                        st.markdown("### Gespeicherte Kontrollpunkte")
                        st.line_chart(pivot)
                    st.download_button("⬇ Forward-Verlauf als CSV",data=hist.to_csv(index=False).encode("utf-8-sig"),file_name="v5_2_forward_competition.csv",mime="text/csv")
            except Exception: pass

        st.markdown("---")
        st.warning("**Wichtig:** Ein Reset zerstört den sauberen Forward-Test. Nur zurücksetzen, wenn du bewusst einen komplett neuen Test beginnen willst.")
        confirm_reset=st.checkbox("Ich möchte den eingefrorenen Forward-Test wirklich verwerfen",key="fw_reset_confirm")
        if st.button("🗑️ Forward-Wettkampf zurücksetzen",disabled=not confirm_reset):
            if FORWARD_COMP_FILE.exists(): FORWARD_COMP_FILE.unlink()
            if FORWARD_HISTORY_FILE.exists(): FORWARD_HISTORY_FILE.unlink()
            for k in ["forward_table","forward_results","forward_equity","forward_eval_at"]: st.session_state.pop(k,None)
            st.success("Forward-Wettkampf zurückgesetzt. Beim nächsten Start wird ein neuer Zeitstempel eingefroren.")

with analytics_tab:
    st.subheader("Wie gut arbeitet dein Paper-Bot wirklich?")
    if LOG_FILE.exists():
        try: trades=pd.read_csv(LOG_FILE)
        except Exception as exc: trades=pd.DataFrame(); st.error(f"Trade-Log konnte nicht gelesen werden: {exc}")
    else: trades=pd.DataFrame()
    if trades.empty:
        st.info("Noch keine Trades vorhanden. Diese Auswertung wird nach den ersten Käufen/Verkäufen automatisch interessant.")
    else:
        summary=analyze_trade_log(trades,start_capital=start_capital); pf="∞" if math.isinf(summary.profit_factor) else f"{summary.profit_factor:.2f}"
        a,b,c,d,e,f=st.columns(6)
        a.metric("Abgeschl. Trades",str(summary.completed_trades)); b.metric("Trefferquote",f"{summary.win_rate_pct:.1f} %")
        c.metric("Netto realisiert",f"{summary.net_pnl:+,.2f} €"); d.metric("Ø Gewinn",f"{summary.avg_win:+,.2f} €")
        e.metric("Ø Verlust",f"{summary.avg_loss:+,.2f} €"); f.metric("Profit Factor",pf)
        g,h,i,j=st.columns(4)
        g.metric("Max. Drawdown*",f"-{summary.max_drawdown_pct:.2f} %"); h.metric("Gebühren protokolliert",f"{summary.total_fees:.2f} €")
        i.metric("Bester Coin",summary.best_coin,f"{summary.best_coin_pnl:+.2f} €" if summary.best_coin!="–" else None)
        j.metric("Schwächster Coin",summary.worst_coin,f"{summary.worst_coin_pnl:+.2f} €" if summary.worst_coin!="–" else None)
        if 0 < summary.completed_trades < 20: st.warning(f"Aktuell erst {summary.completed_trades} abgeschlossene Trades. Für eine erste belastbarere Beurteilung würde ich mindestens 20–30 abwarten.")
        elif summary.completed_trades >= 20: st.success("Es sind jetzt genug abgeschlossene Trades vorhanden, um Einstellungen systematischer miteinander zu vergleichen – trotzdem bleibt die Stichprobe begrenzt.")
        breakdown=coin_breakdown(trades)
        if not breakdown.empty:
            st.markdown("#### Ergebnis nach Coin"); st.dataframe(breakdown, use_container_width=True, hide_index=True)
        if "reason" in trades.columns:
            sells=trades[trades["action"].astype(str).str.upper()=="SELL"]
            if not sells.empty:
                reasons=sells["reason"].fillna("unbekannt").value_counts().rename_axis("Verkaufsgrund").reset_index(name="Anzahl")
                st.markdown("#### Warum wurden Positionen geschlossen?"); st.dataframe(reasons, use_container_width=True, hide_index=True)
        st.caption("* Live-Drawdown wird aus protokollierten Kontowerten bei Trades berechnet und ist deshalb gröber als der Scanner-Backtest. Gebühren älterer importierter V3-Trades können fehlen.")

with backtest_tab:
    st.subheader("Einzelnen Coin und Strategie historisch testen")
    c1,c2,c3=st.columns([1.3,1,1])
    with c1:
        asset=st.selectbox("Coin",list(CRYPTO_UNIVERSE.keys()),index=0); strategy_name=st.selectbox("Strategie",list(STRATEGIES.keys()),index=list(STRATEGIES.keys()).index("Trend + RSI"))
    with c2: start_date=st.date_input("Start",value=date.today()-timedelta(days=365*3),key="bt_start")
    with c3: end_date=st.date_input("Ende",value=date.today(),key="bt_end")
    if end_date>start_date and st.button("Backtest berechnen"):
        try:
            with st.spinner("Backtest läuft …"):
                df=load_history(CRYPTO_UNIVERSE[asset],start_date,end_date+timedelta(days=1)); res_single=backtest(df,STRATEGIES[strategy_name](df),strategy_name,
                    start_capital=start_capital,allocation_pct=allocation,fee_pct=fee,stop_loss_pct=stop_loss,take_profit_pct=take_profit)
            a,b,c,d,e=st.columns(5); a.metric("Start",f"{start_capital:.2f} €"); b.metric("Endwert",f"{res_single.final_value:.2f} €")
            c.metric("Rendite",f"{res_single.return_pct:+.2f} %"); d.metric("Max. Drawdown",f"{res_single.max_drawdown_pct:.2f} %"); e.metric("Trades",str(res_single.trades))
            st.line_chart(pd.DataFrame({"Kurs":df["Close"],"Kapital":res_single.equity}),height=320)
            if not res_single.trade_log.empty: st.dataframe(res_single.trade_log,use_container_width=True,hide_index=True)
        except Exception as exc: st.error(f"Backtest fehlgeschlagen: {exc}")

with explain_tab:
    st.markdown(f"""
### Was V5.2 neu macht

**1. Trailing-Stop:** Nach einem Gewinn von aktuell **{trailing_activation:.1f} %** beginnt der Stop dem bisher höchsten Kurs zu folgen. Bei **{trailing_stop:.1f} %** Trailing-Abstand kann ein Teil eines bereits aufgebauten Gewinns geschützt werden.

**2. Scanner-Backtest:** Mehrere Coins werden rückwirkend genau nacheinander bewertet. Zu jedem historischen Scan werden ausschließlich Daten verwendet, die zu diesem Zeitpunkt bereits vorhanden waren. Der Bot kann damit historisch selbst zwischen Coins auswählen.

**3. Performance-Auswertung:** Trefferquote allein reicht nicht. V4.8 zeigt zusätzlich Profit Factor, durchschnittlichen Gewinn/Verlust, Drawdown, Ergebnis nach Coin und Verkaufsgründe.

**4. Bessere Trade-Dokumentation:** Neue V4.8-Trades speichern zusätzlich technische Werte und Gebühren, damit wir später nachvollziehen können, welche Setups tatsächlich funktioniert haben.

**5. Parameter-Optimierer:** Viele Kombinationen werden systematisch verglichen. Training und Validierung werden getrennt, und die Rangliste bestraft hohen Drawdown, instabile Teilzeiträume und zu wenige Trades.

**6. Marktphasen-Filter:** V4.8 erkennt grob bullische, neutrale und bärische Phasen. Neue Einstiege können im Bärenmarkt blockiert oder nur auf bullische Phasen beschränkt werden. Exits bleiben immer aktiv.

**7. Walk-Forward:** Regeln werden mehrfach nur auf älteren Daten ausgewählt und anschließend auf dem direkt folgenden, vorher ungesehenen Zeitblock geprüft. Das ist strenger als ein einzelner Backtest oder ein einzelner 70/30-Split.

**8. Quality-Breakout + Verlustanalyse:** Der Breakout-Ansatz wird auf strengere, seltenere Einstiege getrimmt. V4.8 zerlegt danach den schlechtesten älteren Suchblock nach Coin, Exit-Grund, Marktphase, RSI, Volumen, Breakout-Stärke, Haltedauer und Verlustserien. Zusätzlich zu Volumen/Trend/Momentum werden Mindest-Edge, mehrere Bestätigungs-Scans, engere Nähe zum Hoch, Mindesthaltezeit und Re-Entry-Cooldowns getestet. Die Rangfolge belohnt 3–4 positive Suchblöcke statt maximaler Einzelrendite.

**9. Hypothesen-Test + Forward-Shadow:** V4.9 prüft nur sechs vorab festgelegte Änderungen gegen dieselbe Referenz. Das Entwicklungs-Ranking sieht den neuesten historischen Bestätigungsabschnitt nicht. Weil frühere Versionen diesen Zeitraum teilweise schon gezeigt haben können, wird er nicht als völlig unangetasteter Final-Holdout verkauft. Stattdessen kannst du den Gewinner ab einem festen Zeitstempel einfrieren; erst danach eintreffende Kurse bilden den echten Forward-Shadow-Test.

**10. Relative-Stärke / Rotation:** V5 rankt die ausgewählten Coins gegeneinander nach 7T-/30T-Momentum, Trend und relativer Volatilität. Getestet werden nur wenige vorab definierte Top-1/Top-2/Top-3-Varianten mit Marktfilter und Rank-Puffer. Ein separater Forward-Shadow kann den Rotations-Gewinner ab jetzt einfrieren.

**11. Defensiver BTC/ETH/Cash-Kern:** V5.1 testet bewusst einfache Trendregeln mit maximal einer Position. Wenn weder BTC noch ETH einen sauberen Langfristtrend erfüllt, bleibt das Kapital in Cash. Es gibt nur wenige vorab definierte Varianten und einen getrennten historischen Bestätigungsabschnitt.

**12. Eingefrorener Forward-Wettkampf:** V5.2 stoppt die historische Strategiesuche bewusst. Quality Breakout, BTC EMA 50/200 und BTC Buy & Hold starten ab einem festen Zeitstempel jeweils mit demselben virtuellen Kapital. Die Regeln werden danach nicht anhand neuer Ergebnisse verändert. Erst nach dem Start eintreffende Kurse zählen; 30 Tage sind das Minimum, 60–90 Tage sind sinnvoller.

### Unsere aktuellen Beispielregeln

Startkapital **{start_capital:.0f} €**, Positionsgröße **{allocation} %**, Stop-Loss **{stop_loss:.1f} %**, Take-Profit **{take_profit:.1f} %**, Trailing **{trailing_stop:.1f} %** ab **+{trailing_activation:.1f} %**.
""")
    st.warning("Paper-Trading, Backtests und Optimierung garantieren keine zukünftigen Gewinne. V5.1 sendet weiterhin keine echten Bitpanda-Orders.")
