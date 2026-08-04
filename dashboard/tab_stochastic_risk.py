import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from dashboard.stochastic_risk_service import (
    DEFAULT_N_PORTFOLIOS,
    KNOWN_SECTORS,
    build_markov_projection_df,
    build_sector_shock_curve,
    resolve_sector,
    run_bernoulli_shock,
    run_mc_portfolio_impact,
    run_sector_shock,
)


@st.cache_data(ttl=300)
def get_cached_mc_simulation(
    current_spread_bps=None,
    regime="NORMAL",
    n_portfolios=DEFAULT_N_PORTFOLIOS,
    portfolio_weights=None,
):
    return run_mc_portfolio_impact(
        current_spread_bps=current_spread_bps,
        regime=regime,
        n_portfolios=n_portfolios,
        portfolio_weights=portfolio_weights,
    )


def render_stochastic_risk_tab(primary_ticker: str):
    st.title("🎲 Stochastic Risk & Monte Carlo")
    st.caption("Poisson black-swan, Markov lifecycle, and Bernoulli credit-shock analytics from the Quantitative/stochastic engine.")

    st.markdown("---")

    # 1. Monte Carlo / Poisson Black-Swan Portfolio Impact
    st.markdown("### 📉 Monte Carlo / Poisson Black-Swan Portfolio Impact")
    st.caption("Shock arrival intensity λ scales with the current BAA10Y credit spread; each portfolio sim is a Poisson draw with LogNormal drawdown severities.")

    c1, c2, c3 = st.columns(3)
    with c1:
        regime = st.selectbox("Credit Spread Regime", ["NORMAL", "WIDENING", "CRISIS", "UNKNOWN"], key="stoch_regime")
    with c2:
        spread_bps = st.slider("Current BAA10Y Spread (bps)", 100, 600, 220, step=10, key="stoch_spread")
    with c3:
        n_portfolios = st.slider("Monte Carlo Portfolio Simulations", 100, 2000, DEFAULT_N_PORTFOLIOS, step=100, key="stoch_nsims")

    mc = get_cached_mc_simulation(current_spread_bps=float(spread_bps), regime=regime, n_portfolios=n_portfolios)

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Expected Shocks / Year (λ)", f"{mc['lambda_stress']:.2f}")
    with m2:
        st.metric("Total Simulated Shocks", f"{mc['n_shocks']}")
    with m3:
        st.metric("Mean Portfolio Impact", f"{mc['portfolio_impact']:.2%}")
    with m4:
        st.metric("MC Compute Time", f"{mc['compute_ms']:.1f} ms")

    if mc["shock_magnitudes"]:
        fig = go.Figure()
        fig.add_trace(go.Histogram(
            x=mc["shock_magnitudes"],
            nbinsx=30,
            marker_color="#00ffd0",
        ))
        fig.update_layout(
            title="Simulated Black-Swan Drawdown Distribution",
            xaxis_title="Drawdown Magnitude",
            yaxis_title="Frequency",
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(color='#ffffff'),
            margin=dict(t=30, b=10, l=10, r=10),
            height=320,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No black-swan shocks fired under the current regime and spread.")

    st.markdown("---")

    # 2. Markov Lifecycle Projection
    st.markdown("### 🔄 Markov Lifecycle Projection")
    st.caption("State probabilities projected forward 5 years from the current Peter Lynch lifecycle classification.")

    proj_df, markov = build_markov_projection_df(primary_ticker)

    l1, l2, l3, l4 = st.columns(4)
    with l1:
        st.metric("Current Lifecycle State", markov["current_state"])
    with l2:
        st.metric("Projected State (5y)", markov["projected_state"])
    with l3:
        st.metric("Transition Volatility", f"{markov['transition_volatility']:.3f}")
    with l4:
        st.metric("Convergence Step", f"{markov['convergence_step']}")

    fig = go.Figure()
    colors = ["#00ffd0", "#0099ff", "#ffaa00", "#7000ff", "#ff007f", "#00ff88"]
    for idx, state in enumerate(proj_df.columns[1:]):
        fig.add_trace(go.Scatter(
            x=proj_df["step"],
            y=proj_df[state],
            mode="lines+markers",
            name=state,
            line=dict(color=colors[idx % len(colors)]),
        ))
    fig.update_layout(
        title="Lifecycle State Probability Trajectories",
        xaxis_title="Years Ahead",
        yaxis_title="Probability",
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#ffffff'),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=30, b=10, l=10, r=10),
        height=380,
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("View Lifecycle Projection Table"):
        st.dataframe(proj_df, use_container_width=True, hide_index=True)

    st.markdown("---")

    # 3. Bernoulli Shock Default Probability
    st.markdown("### 💥 Bernoulli Shock Default Probability")
    st.caption("Damodaran ICR → synthetic rating → empirical default probability mapping with a single Bernoulli trial.")

    default_icr = float(markov["metrics_used"]["interest_coverage_ratio"])
    default_icr = min(max(default_icr, 0.0), 40.0)

    b1, b2, b3 = st.columns(3)
    with b1:
        icr = st.slider("Interest Coverage Ratio (ICR)", 0.0, 40.0, default_icr, step=0.5, key="stoch_icr")
    with b2:
        supplier_concentration = st.slider("Supplier Concentration", 0.0, 1.0, 0.5, step=0.05, key="stoch_sup")
    with b3:
        geo_stress = st.slider("Geopolitical Stress Factor", 0.0, 1.0, 0.0, step=0.05, key="stoch_geo")

    ber = run_bernoulli_shock(
        icr=icr,
        supplier_concentration=supplier_concentration,
        geopolitical_stress_factor=geo_stress,
    )

    d1, d2, d3, d4 = st.columns(4)
    with d1:
        st.metric("Synthetic Rating", ber["synthetic_rating"])
    with d2:
        st.metric("1Y Default Probability", f"{ber['p_default_1y']:.4%}")
    with d3:
        st.metric("5Y Default Probability", f"{ber['p_default_5y']:.4%}")
    with d4:
        st.metric("Effective Shock Probability", f"{ber['shock_probability']:.4%}")

    ber_rows = [
        {"Metric": "Shock Fired in Trial", "Value": "Yes" if ber["shock_occurred"] else "No"},
        {"Metric": "FCFE Penalty Multiplier", "Value": f"{ber['penalty_multiplier']:.4f}"},
        {"Metric": "Loss Given Default (LGD)", "Value": f"{ber['lgd']:.4f}"},
        {"Metric": "Recovery Rate", "Value": f"{ber['recovery_rate']:.2%}"},
    ]
    st.dataframe(pd.DataFrame(ber_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # 4. Dynamic Sector Shock Probability
    st.markdown("### 🏭 Dynamic Sector Shock Probability")
    st.caption("p_shock = p_base × (σ_margin_TTM / σ_margin_10Y) with Bayesian-shrunk sector base rates.")

    default_sector = resolve_sector(primary_ticker)
    sector_idx = KNOWN_SECTORS.index(default_sector) if default_sector in KNOWN_SECTORS else 0
    sector = st.selectbox("Sector", KNOWN_SECTORS, index=sector_idx, key="stoch_sector")

    s1, s2 = st.columns(2)
    with s1:
        sup_curve = st.slider("Supplier Concentration (Sector)", 0.0, 1.0, 0.5, step=0.05, key="stoch_sector_sup")
    with s2:
        geo_curve = st.slider("Geopolitical Stress Factor (Sector)", 0.0, 1.0, 0.0, step=0.05, key="stoch_sector_geo")

    sector_res = run_sector_shock(
        sector=sector,
        current_margin_vol=markov["metrics_used"]["margin_variance_10y"],
        supplier_concentration=sup_curve,
        geopolitical_stress_factor=geo_curve,
    )

    e1, e2, e3 = st.columns(3)
    with e1:
        st.metric("Sector Base Rate (p_base)", f"{sector_res['p_base']:.4%}")
    with e2:
        st.metric("10Y Margin Vol Reference", f"{sector_res['margin_vol_10y']:.2%}")
    with e3:
        st.metric("Dynamic Shock Probability", f"{sector_res['shock_probability']:.4%}")

    curve = build_sector_shock_curve(
        sector=sector,
        supplier_concentration=sup_curve,
        geopolitical_stress_factor=geo_curve,
    )
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=curve["current_margin_vol"],
        y=curve["shock_probability"],
        mode="lines+markers",
        name="p_shock",
        line=dict(color="#ffaa00"),
        marker=dict(color="#ffaa00"),
    ))
    fig.update_layout(
        title="Shock Probability vs TTM Margin Volatility",
        xaxis_title="Current Margin Volatility (σ_TTM)",
        yaxis_title="Shock Probability",
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#ffffff'),
        margin=dict(t=30, b=10, l=10, r=10),
        height=320,
    )
    st.plotly_chart(fig, use_container_width=True)
