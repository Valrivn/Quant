import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import time
import os
from datetime import datetime

# Import modular components
from db.connection import get_db_connection
from db.feature_store import FeatureStore, get_sentiment_features
from scraper.risk_detector import detect_risk_narratives
from scraper.engine import QuantSentimentEngine
from optimization.ab_testing import (
    get_champion_weights, get_latest_challenger, 
    promote_challenger_to_champion, compare_champion_vs_challenger
)
from optimization.optuna_search import run_bayesian_optimization, save_optimized_weights_as_challenger

# Caching DB Pool Connection
@st.cache_resource
def get_cached_connection():
    """Cache connection pool resource."""
    return get_db_connection()

@st.cache_data(ttl=300)
def get_cached_sentiment_features(ticker: str):
    """Cache daily sentiment features for 300 seconds."""
    from datetime import datetime, timedelta, timezone
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    return get_sentiment_features(ticker, start_date, end_date)

@st.cache_data(ttl=300)
def get_cached_risk_narratives():
    """Cache active risk narratives for 300 seconds."""
    return detect_risk_narratives()

def render_sentiment_risk_tab(primary_ticker: str):
    st.title("Social Sentiment & Risk Analytics")
    
    # 30-second Auto Refresh Checkbox
    auto_refresh = st.checkbox("Enable 30s Auto-Refresh", value=False)
    if auto_refresh:
        st.info("🔄 Auto-refresh enabled. Page will reload every 30 seconds.")
        time.sleep(30)
        st.rerun()

    # 1. Error & Status UI Header (System Health Indicators)
    st.markdown("### 🖥️ System Health & Version Indicators")
    
    engine = QuantSentimentEngine()
    model_info = engine.get_model_version()
    champ_weights, champ_ver = get_champion_weights()
    
    h1, h2, h3, h4 = st.columns(4)
    with h1:
        st.metric("Model Version", model_info["model_version"].split("-")[-1], help=model_info["model_version"])
    with h2:
        st.metric("Active Weight Version", f"v{champ_ver}" if champ_ver else "v1.0 (Local YAML)")
    with h3:
        # Rate limit simulation warning/status
        st.metric("Scraper Request Status", "Healthy ✅", delta="0 skipped subs")
    with h4:
        st.metric("API Quota / Rotation", "Active (API-Free)", delta="2.0s Throttle")

    st.markdown("---")

    # 2. Sentiment Feature Store & Stacked Charts
    st.markdown("### 📈 Sentiment Feature Store Breakdown")
    st.caption("Pivoted sentiment coordinates pulled directly from the per-category sentiment Feature Store.")
    
    df_features = get_cached_sentiment_features(primary_ticker)
    
    if not df_features.empty and len(df_features) > 1:
        st.dataframe(df_features.tail(10), use_container_width=True)
        
        # Plot stacked sentiment by category over time
        st.subheader("Category Sentiment Contributions over Time")
        fig = go.Figure()
        categories = ['macro_geopolitical', 'fundamental_institutional', 'tech_product', 'retail_options']
        
        colors = ['#ffaa00', '#00ffd0', '#0099ff', '#7000ff']
        for idx, cat in enumerate(categories):
            if cat in df_features.columns:
                fig.add_trace(go.Bar(
                    name=cat,
                    x=df_features.index,
                    y=df_features[cat],
                    marker_color=colors[idx]
                ))
                
        fig.update_layout(
            barmode='relative',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(color='#ffffff'),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=30, b=10, l=10, r=10),
            height=350
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(f"No historical sentiment features found in store for `{primary_ticker}` yet. Run the scraper to populate daily records.")

    st.markdown("---")

    # 3. Risk Narrative Detection
    st.markdown("### ⚠️ Active Geopolitical & Supply Chain Risk Anomalies")
    st.caption("Trending risk narratives flagged when the daily keyword frequency Z-score exceeds 1.5.")
    
    df_trends = get_cached_risk_narratives()
    if not df_trends.empty:
        st.dataframe(
            df_trends[["date", "category", "risk_type", "frequency", "mean", "z_score"]],
            use_container_width=True
        )
        
        # Plot anomalies
        fig_risk = go.Figure()
        for rtype in df_trends['risk_type'].unique():
            df_sub = df_trends[df_trends['risk_type'] == rtype]
            fig_risk.add_trace(go.Scatter(
                x=df_sub['date'],
                y=df_sub['z_score'],
                mode='markers+lines',
                name=rtype,
                marker=dict(size=10)
            ))
        fig_risk.update_layout(
            title="Risk Signal Z-Scores",
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(color='#ffffff'),
            height=300
        )
        st.plotly_chart(fig_risk, use_container_width=True)
    else:
        st.info("No active risk narratives detected (all Z-scores stable).")

    st.markdown("---")

    # 4. Weight Optimization & A/B Testing Panel
    st.markdown("### 🧬 Bayesian Weight Optimizer & A/B Sandbox")
    st.caption("Compare Champion (Current Active) weights against Challenger (Optuna Optimized) configurations.")
    
    comp = compare_champion_vs_challenger()
    
    ab1, ab2 = st.columns(2)
    
    with ab1:
        st.markdown("#### Champion Performance (Active)")
        if comp["champion"]:
            st.write(f"**Version ID:** {comp['champion']['version_id']}")
            st.metric("Sharpe Ratio", f"{comp['champion']['sharpe']:.2f}")
            st.metric("Information Coefficient (IC)", f"{comp['champion']['ic']:.4%}")
            st.metric("Hit Rate", f"{comp['champion']['hit_rate']:.1%}")
        else:
            st.info("No active champion version in SQLite DB yet. Run optimization to promote one.")

    with ab2:
        st.markdown("#### Challenger Performance (Latest Optuna Output)")
        if comp["challenger"]:
            st.write(f"**Version ID:** {comp['challenger']['version_id']}")
            st.metric("Sharpe Ratio", f"{comp['challenger']['sharpe']:.2f}")
            st.metric("Information Coefficient (IC)", f"{comp['challenger']['ic']:.4%}")
            st.metric("Hit Rate", f"{comp['challenger']['hit_rate']:.1%}")
            
            # Promotion Button
            if st.button("🚀 Promote Challenger to Champion"):
                promote_challenger_to_champion(comp['challenger']['version_id'])
                st.success(f"Successfully promoted Version {comp['challenger']['version_id']} to active Champion!")
                st.rerun()
        else:
            st.info("No challenger version found. Click below to run Optuna search.")

    st.markdown("---")
    st.markdown("#### Run Bayesian Weight Search")
    trials = st.slider("Optuna Search Trials", min_value=5, max_value=100, value=20)
    metric_opt = st.selectbox("Optimization Metric", ["sharpe", "information_coefficient"])
    
    if st.button("🎯 Trigger Optuna Optimization"):
        with st.spinner("Running Optuna Bayesian Optimization over historical backtest..."):
            res = run_bayesian_optimization(trials=trials, objective_metric=metric_opt)
            save_optimized_weights_as_challenger(res)
            st.success("Optimization run completed! New challenger weights saved to database.")
            st.rerun()
