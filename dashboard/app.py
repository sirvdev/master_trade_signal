"""
dashboard/app.py
================
Streamlit dashboard for the signal bot.
Run with: streamlit run dashboard/app.py --server.port 8501
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path so imports work when run from dashboard/
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import streamlit as st

from config import ChannelConfig, load_config, save_channels
from db.database import Database

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title = "Signal Bot Dashboard",
    page_icon  = "📡",
    layout     = "wide",
    initial_sidebar_state = "expanded",
)

# ── Load config & DB ──────────────────────────────────────────────────────────
@st.cache_resource
def get_db():
    cfg = load_config()
    return Database(cfg.db_path), cfg

db, cfg = get_db()


# ── Sidebar navigation ────────────────────────────────────────────────────────
st.sidebar.title("📡 Signal Bot")
st.sidebar.markdown("---")
page = st.sidebar.radio("Navigate", [
    "📊 Overview",
    "📺 Live Positions",
    "⚙️ Channel Config",
    "💰 Balances",
    "📈 Reports",
    "🏆 Performance",
    "📋 Logs",
])
st.sidebar.markdown("---")
st.sidebar.caption(f"Last refresh: {datetime.utcnow().strftime('%H:%M:%S')} UTC")
if st.sidebar.button("🔄 Refresh"):
    st.cache_resource.clear()
    st.rerun()


# ═══════════════════════════════════════════════════════════
# Overview
# ═══════════════════════════════════════════════════════════
if page == "📊 Overview":
    st.title("📊 Overview")

    # Summary metrics
    all_trades = db.get_trades_report()
    df_all     = pd.DataFrame([dict(r) for r in all_trades]) if all_trades else pd.DataFrame()

    # Today's trades
    today = datetime.utcnow().date().isoformat()
    today_trades = [r for r in all_trades
                    if str(r["opened_at"] or "")[:10] == today] if all_trades else []

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Closed Trades", len(all_trades))
    with col2:
        st.metric("Today's Trades", len(today_trades))
    with col3:
        open_pos = db.get_all_open_positions()
        st.metric("Open Positions", len(open_pos))
    with col4:
        total_pnl = sum(float(r["pnl"] or 0) for r in all_trades)
        st.metric("Total P&L", f"${total_pnl:.2f}",
                  delta=f"${sum(float(r['pnl'] or 0) for r in today_trades):.2f} today")

    st.markdown("---")

    # Channel status
    st.subheader("Channel Status")
    ch_cols = st.columns(max(1, len(cfg.channels)))
    for i, ch in enumerate(cfg.channels):
        with ch_cols[i % len(ch_cols)]:
            status = "🔴 HALTED" if ch.halted else ("🟢 Active" if ch.enabled else "⚫ Disabled")
            st.markdown(f"**{ch.name}**")
            st.caption(f"{status} | Risk: {ch.risk_pct}% | DD limit: {ch.drawdown_pct}%")
            today_stats = db.get_today_stats(ch.id)
            if today_stats:
                dd = float(today_stats["drawdown_pct"] or 0)
                st.progress(min(1.0, dd / ch.drawdown_pct),
                            text=f"Drawdown: {dd:.1f}% / {ch.drawdown_pct}%")

    # Equity curve (all time)
    if not df_all.empty and "opened_at" in df_all.columns:
        st.markdown("---")
        st.subheader("Equity Curve (cumulative P&L)")
        df_eq = df_all[["opened_at", "pnl"]].copy()
        df_eq["opened_at"] = pd.to_datetime(df_eq["opened_at"], errors="coerce")
        df_eq = df_eq.sort_values("opened_at").dropna()
        df_eq["cumulative_pnl"] = df_eq["pnl"].cumsum()
        st.line_chart(df_eq.set_index("opened_at")["cumulative_pnl"])


# ═══════════════════════════════════════════════════════════
# Live Positions
# ═══════════════════════════════════════════════════════════
elif page == "📺 Live Positions":
    st.title("📺 Live Positions")

    open_pos = db.get_all_open_positions()
    if not open_pos:
        st.info("No open positions")
    else:
        rows = []
        for p in open_pos:
            sig = db.get_signal(p["signal_id"])
            rows.append({
                "Ticket":    p["ticket"],
                "Channel":   p["channel_id"],
                "Symbol":    sig["symbol"] if sig else "?",
                "Direction": sig["direction"] if sig else "?",
                "TP":        f"TP{p['tp_index']}",
                "Entry":     p["entry_price"],
                "SL":        p["stop_loss"],
                "TP Price":  p["tp_price"],
                "Lots":      p["lot_size"],
                "P&L":       f"${float(p['pnl'] or 0):.2f}",
                "Opened":    str(p["opened_at"] or "")[:16],
            })
        df = pd.DataFrame(rows)
        # Colour P&L column
        st.dataframe(df, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════
# Channel Config
# ═══════════════════════════════════════════════════════════
elif page == "⚙️ Channel Config":
    st.title("⚙️ Channel Configuration")
    st.caption("Changes saved to channels.json — no restart needed for new signals")

    # Edit existing channels
    updated = []
    for i, ch in enumerate(cfg.channels):
        with st.expander(f"{'🟢' if ch.enabled else '⚫'} {ch.name} ({ch.id})",
                         expanded=False):
            c1, c2 = st.columns(2)
            with c1:
                name   = st.text_input("Name",        ch.name,   key=f"name_{i}")
                ch_id  = st.text_input("Channel ID",  ch.id,     key=f"id_{i}")
                symbol = st.text_input("Symbol",      ch.symbol, key=f"sym_{i}")
            with c2:
                risk   = st.slider("Risk %",          1.0, 50.0, ch.risk_pct,   0.5, key=f"risk_{i}")
                dd     = st.slider("Drawdown limit %",5.0, 50.0, ch.drawdown_pct,1.0, key=f"dd_{i}")
                pre    = st.number_input("Pre-ann positions", 1, 5, ch.pre_ann_positions, key=f"pre_{i}")
                enabled = st.checkbox("Enabled", ch.enabled, key=f"en_{i}")
                start_bal = st.number_input("Starting balance ($, 0 = disabled)",
                                             0.0, 1_000_000.0, ch.starting_balance,
                                             step=10.0, key=f"sb_{i}")
                drift = st.slider("Balance drift warning %", 1.0, 50.0,
                                   ch.balance_drift_pct, 0.5, key=f"drift_{i}")

            st.caption("Session risk multipliers (1.0 = no change)")
            sm1, sm2, sm3 = st.columns(3)
            with sm1:
                asian_m  = st.slider("Asian (02-07 UTC)",  0.0, 2.0,
                                      ch.asian_risk_mult,  0.05, key=f"am_{i}")
            with sm2:
                london_m = st.slider("London (07-13 UTC)", 0.0, 2.0,
                                      ch.london_risk_mult, 0.05, key=f"lm_{i}")
            with sm3:
                ny_m     = st.slider("NY (13-21 UTC)",     0.0, 2.0,
                                      ch.ny_risk_mult,     0.05, key=f"nm_{i}")

            updated.append(ChannelConfig(
                id=ch_id, name=name, symbol=symbol,
                risk_pct=risk, drawdown_pct=dd,
                pre_ann_positions=int(pre), enabled=enabled,
                starting_balance=start_bal, balance_drift_pct=drift,
                asian_risk_mult=asian_m, london_risk_mult=london_m,
                ny_risk_mult=ny_m,
            ))

    # Add new channel
    st.markdown("---")
    st.subheader("Add Channel")
    nc1, nc2 = st.columns(2)
    with nc1:
        new_id   = st.text_input("Channel ID (e.g. -1001234567890)")
        new_name = st.text_input("Channel Name")
        new_sym  = st.text_input("Symbol", "XAUUSD")
    with nc2:
        new_risk = st.slider("Risk %",          1.0, 50.0, 10.0, 0.5)
        new_dd   = st.slider("Drawdown limit %",5.0, 50.0, 20.0, 1.0)
        new_pre  = st.number_input("Pre-ann positions", 1, 5, 1)

    if st.button("➕ Add Channel") and new_id and new_name:
        updated.append(ChannelConfig(
            id=new_id, name=new_name, symbol=new_sym,
            risk_pct=new_risk, drawdown_pct=new_dd,
            pre_ann_positions=int(new_pre), enabled=True,
            starting_balance=0.0, balance_drift_pct=5.0,
            asian_risk_mult=1.0, london_risk_mult=1.0, ny_risk_mult=1.0,
        ))
        st.success(f"Channel '{new_name}' added")

    if st.button("💾 Save All Changes", type="primary"):
        save_channels(updated)
        cfg.channels = updated
        st.success("Saved to channels.json")
        st.cache_resource.clear()


# ═══════════════════════════════════════════════════════════
# Balances (system_balance ledger)
# ═══════════════════════════════════════════════════════════
elif page == "💰 Balances":
    st.title("💰 Channel Balances")
    st.caption("Per-channel system_balance ledger. Sizing uses min(equity, system_balance).")

    if not cfg.channels:
        st.info("No channels configured.")
    else:
        # Try to get live equity once (best-effort, no MT5 in dashboard process)
        live_equity = None
        try:
            from bridge.mt5_bridge import MT5FileBridge  # noqa
            # Dashboard runs in its own process — we cannot call async bridge here.
            # Fall back to today's most recent equity stat.
        except Exception:
            pass

        rows = []
        for ch in cfg.channels:
            rec = db.get_system_balance(ch.id)
            today_stats = db.get_today_stats(ch.id)
            cur_eq = float(today_stats["current_equity"]) if today_stats and today_stats["current_equity"] else None

            if rec:
                start_bal = rec["starting_balance"]
                sys_bal   = rec["system_balance"]
                last_upd  = rec["last_updated"]
            else:
                start_bal = ch.starting_balance
                sys_bal   = None
                last_upd  = "—"

            drift = None
            if sys_bal and cur_eq:
                drift = abs(cur_eq - sys_bal) / max(sys_bal, 1.0) * 100

            rows.append({
                "Channel":          ch.name,
                "Starting":         f"${start_bal:.2f}" if start_bal else "—",
                "System Balance":   f"${sys_bal:.2f}" if sys_bal is not None else "(not initialised)",
                "Account Equity":   f"${cur_eq:.2f}" if cur_eq is not None else "—",
                "Drift %":          f"{drift:.2f}%" if drift is not None else "—",
                "Last Updated":     str(last_upd)[:19],
            })

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.subheader("30-Day Win Rate")
        for ch in cfg.channels:
            st.markdown(f"**{ch.name}**")
            wr = db.get_channel_winrate(ch.id, days=30)
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("30-day WR", f"{wr['wr_pct']:.1f}%")
            col2.metric("Wins", wr['wins'])
            col3.metric("Losses", wr['losses'])
            col4.metric("Scratches", wr['scratches'])

            # Warning if WR drops below survival threshold for high-risk channels
            if ch.risk_pct >= 15 and wr['total'] >= 10 and wr['wr_pct'] < 63:
                st.warning(
                    f"⚠️ Win rate {wr['wr_pct']:.1f}% is below the ~63% survival "
                    f"threshold for {ch.risk_pct}% risk. Account will lose money "
                    f"in expectation if this rate continues."
                )

        st.markdown("---")
        st.subheader("Reset System Balance")
        for i, ch in enumerate(cfg.channels):
            today_stats = db.get_today_stats(ch.id)
            cur_eq = float(today_stats["current_equity"]) if today_stats and today_stats["current_equity"] else 0.0
            col_a, col_b, col_c = st.columns([3, 2, 2])
            col_a.write(f"**{ch.name}**")
            new_val = col_b.number_input(
                f"New starting balance ($)",
                0.0, 1_000_000.0,
                value=float(cur_eq) if cur_eq > 0 else float(ch.starting_balance),
                step=10.0, key=f"reset_val_{i}",
            )
            if col_c.button("Reset", key=f"reset_btn_{i}"):
                db.reset_system_balance(ch.id, new_starting=float(new_val))
                st.success(f"Reset {ch.name} system balance to ${new_val:.2f}")


# ═══════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════
elif page == "📈 Reports":
    st.title("📈 Reports")

    # Filters
    f1, f2, f3 = st.columns(3)
    with f1:
        ch_options = ["All"] + [ch.name for ch in cfg.channels]
        sel_ch     = st.selectbox("Channel", ch_options)
    with f2:
        from_date = st.date_input("From", datetime.utcnow().date() - timedelta(days=30))
    with f3:
        to_date   = st.date_input("To",   datetime.utcnow().date())

    ch_id_filter = None
    if sel_ch != "All":
        ch_id_filter = next((ch.id for ch in cfg.channels if ch.name == sel_ch), None)

    trades = db.get_trades_report(
        channel_id = ch_id_filter,
        from_date  = from_date.isoformat(),
        to_date    = (to_date + timedelta(days=1)).isoformat(),
    )

    if not trades:
        st.info("No closed trades in selected range")
    else:
        df = pd.DataFrame([dict(r) for r in trades])
        df["pnl"] = df["pnl"].astype(float)
        df["opened_at"] = pd.to_datetime(df["opened_at"], errors="coerce")

        # Metrics
        wins    = (df["pnl"] > 0).sum()
        losses  = (df["pnl"] <= 0).sum()
        total   = len(df)
        wr      = wins / total * 100 if total else 0
        net_pnl = df["pnl"].sum()
        avg_pnl = df["pnl"].mean()

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Trades",    total)
        m2.metric("Win Rate",  f"{wr:.1f}%")
        m3.metric("Net P&L",   f"${net_pnl:.2f}")
        m4.metric("Avg P&L",   f"${avg_pnl:.2f}")
        m5.metric("Best",      f"${df['pnl'].max():.2f}")

        # Equity curve
        st.markdown("---")
        df_sorted = df.sort_values("opened_at").copy()
        df_sorted["cumulative"] = df_sorted["pnl"].cumsum()
        st.subheader("Equity Curve")
        st.line_chart(df_sorted.set_index("opened_at")["cumulative"])

        # Trade table
        st.markdown("---")
        st.subheader("Trade Log")
        display_cols = ["opened_at", "channel_name", "symbol", "direction",
                        "lot_size", "entry_price", "pnl", "close_reason"]
        display_cols = [c for c in display_cols if c in df.columns]
        st.dataframe(df[display_cols].sort_values("opened_at", ascending=False),
                     use_container_width=True, hide_index=True)

        # Download
        csv = df.to_csv(index=False)
        st.download_button("⬇️ Download CSV", csv, "trades.csv", "text/csv")


# ═══════════════════════════════════════════════════════════
# Performance
# ═══════════════════════════════════════════════════════════
elif page == "🏆 Performance":
    st.title("🏆 Channel Performance")

    summary = db.get_channel_summary()
    if not summary:
        st.info("No closed trades yet")
    else:
        rows = [dict(r) for r in summary]
        df   = pd.DataFrame(rows)
        df["win_rate"] = (df["wins"] / df["total_trades"] * 100).round(1)

        # Rank by total_pnl
        df = df.sort_values("total_pnl", ascending=False).reset_index(drop=True)
        df["rank"] = df.index + 1

        # Medal emojis
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        df["#"] = df["rank"].map(lambda r: medals.get(r, str(r)))

        st.dataframe(
            df[["#", "channel_name", "total_trades", "wins", "losses",
                "win_rate", "avg_pnl", "total_pnl"]].rename(columns={
                    "channel_name": "Channel",
                    "total_trades": "Trades",
                    "wins":         "Wins",
                    "losses":       "Losses",
                    "win_rate":     "Win Rate %",
                    "avg_pnl":      "Avg P&L $",
                    "total_pnl":    "Total P&L $",
                }),
            use_container_width=True, hide_index=True
        )

        st.markdown("---")
        st.subheader("P&L by Channel")
        st.bar_chart(df.set_index("channel_name")["total_pnl"])

        # Highlight bad performers
        bad = df[df["win_rate"] < 40]
        if not bad.empty:
            st.warning(
                f"⚠️ Underperforming channels (<40% win rate): "
                f"{', '.join(bad['channel_name'].tolist())}"
            )


# ═══════════════════════════════════════════════════════════
# Logs
# ═══════════════════════════════════════════════════════════
elif page == "📋 Logs":
    st.title("📋 Logs")

    log_dir = Path(cfg.log_dir)
    log_files = list(log_dir.glob("*.log")) + list((log_dir / "channels").glob("*.log"))
    log_files = sorted(log_files, key=lambda p: p.stat().st_mtime, reverse=True)

    if not log_files:
        st.info("No log files found")
    else:
        selected = st.selectbox("Log file", [p.name for p in log_files])
        log_path = next(p for p in log_files if p.name == selected)

        lines_count = st.slider("Lines to show (from end)", 50, 2000, 200, 50)
        search_term = st.text_input("Filter (contains)")

        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            lines = all_lines[-lines_count:]
            if search_term:
                lines = [l for l in lines if search_term.lower() in l.lower()]

            content = "".join(lines)
            st.text_area("Log content", content, height=500)
            st.caption(f"{log_path} — {len(all_lines)} total lines")
        except Exception as e:
            st.error(f"Cannot read log: {e}")