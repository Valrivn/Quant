import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from valuation_alpha.pipeline import run_live_full

_L1_COLS = [
    "ticker", "sector", "lifecycle", "mahalanobis",
    "alpha_1y_ann", "alpha_1y_ci_lower", "alpha_1y_ci_upper",
    "alpha_3y_ann", "alpha_3y_ci_lower", "alpha_3y_ci_upper",
]


@st.cache_data(ttl=300)
def get_cached_pipeline():
    return run_live_full()


def _read_report(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _l1_frame(names):
    present = [c for c in _L1_COLS if c in names.columns]
    return names[present]


def render_valuation_alpha_tab():
    st.title("🏆 Relative-Alpha Valuation")
    st.caption(
        "End-to-end D-20260801-004 pipeline: L1 equity sleeve -> selection -> "
        "bias ablation -> L2 diversification -> L3 allocator."
    )
    try:
        result = get_cached_pipeline()
    except Exception as e:
        st.warning(f"Relative-alpha pipeline failed: {e}")
        return

    reports = result.get("reports_paths", {})

    st.markdown("### 📊 Equity Sleeve (L1)")
    run_a = result.get("run_a", {})
    run_b = result.get("run_b", {})
    names_a = run_a.get("names")
    names_b = run_b.get("names")
    if names_a is not None and not names_a.empty:
        st.dataframe(_l1_frame(names_a), use_container_width=True)
    else:
        st.info("No Run A names available.")
    st.markdown("**Run B (no megacap bias):**")
    if names_b is not None and not names_b.empty:
        st.dataframe(_l1_frame(names_b), use_container_width=True)
    else:
        st.info("No Run B names available.")
    st.markdown(_read_report(reports.get("bias_ablation", "")))

    st.markdown("### 🎯 Candidate Ranking")
    ranking = result.get("ranking")
    if ranking is not None and not ranking.empty:
        st.dataframe(ranking, use_container_width=True)
    else:
        st.info("No candidate ranking available.")

    st.markdown("### 🛡️ Diversification Sleeve (L2)")
    st.markdown(_read_report(reports.get("sleeve_backtest", "")))
    sleeve_results = result.get("sleeve_results")
    if sleeve_results is not None and not sleeve_results.empty:
        fig = go.Figure(go.Bar(
            x=sleeve_results["sleeve"],
            y=sleeve_results["deflated_sharpe"],
            marker_color="#00ffd0",
        ))
        fig.update_layout(
            title="Deflated Sharpe per Sleeve",
            xaxis_title="Sleeve",
            yaxis_title="Deflated Sharpe",
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(color='#ffffff'),
            margin=dict(t=30, b=10, l=10, r=10),
            height=320,
        )
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("### 🏦 Whole Portfolio (L3)")
    st.markdown(_read_report(reports.get("allocator", "")))
    allocator = result.get("allocator")
    if allocator and "backtest" in allocator:
        backtest = allocator["backtest"]
        returns = backtest.get("returns")
        if returns is not None and len(returns):
            cum = (1.0 + returns).cumprod()
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=cum.index, y=cum, mode="lines", name="Portfolio",
                line=dict(color="#00ffd0"),
            ))
            fig.update_layout(
                title="Cumulative Portfolio Returns",
                xaxis_title="Date",
                yaxis_title="Cumulative Return",
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                font=dict(color='#ffffff'),
                margin=dict(t=30, b=10, l=10, r=10),
                height=320,
            )
            st.plotly_chart(fig, use_container_width=True)