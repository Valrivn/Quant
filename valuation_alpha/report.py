"""P2 bias-ablation report rendering for the CEO."""


def bias_ablation_report(all_50: dict, non_mega_40: dict, stats: dict) -> str:
    """Render the P2 comparison report as markdown text.

    all_50 and non_mega_40 are per-group stat dicts; stats is the bias_ablation
    result containing run_a, run_b, and verdict. Returns a plain string with no
    file I/O.
    """
    verdict = stats["verdict"]
    if verdict == "EDGE_REAL":
        recommendation = (
            "KEEP the megacap bias: the non-megacap cohort retains positive "
            "alpha, so the edge does not depend on megacaps alone."
        )
    elif verdict == "RIDING_BIAS":
        recommendation = (
            "DROP the megacap bias: alpha is concentrated entirely in megacaps "
            "and the non-megacap subset shows no edge."
        )
    elif verdict == "NEW_SECTORS":
        recommendation = (
            "KEEP the megacap bias but re-weight: the edge now lives in "
            "non-megacap sectors, so the P4 optimizer should favor them."
        )
    else:
        recommendation = (
            "INCONCLUSIVE: neither cohort shows a reliable edge; re-examine "
            "before the P4 optimizer runs."
        )

    ra = stats["run_a"]
    rb = stats["run_b"]
    lines = [
        "# P2 Bias-Ablation Report",
        "",
        "## Run A (all 50 names)",
        "",
        f"- Mean alpha_3y_ann: {ra['mean_alpha_3y']:.4f}",
        f"- Share of positive-alpha names: {ra['share_positive']:.2%}",
        f"- Best name: {ra['best']}",
        f"- Worst name: {ra['worst']}",
        "",
        "## Run B (40 non-megacap names)",
        "",
        f"- Mean alpha_3y_ann: {rb['mean_alpha_3y']:.4f}",
        f"- Share of positive-alpha names: {rb['share_positive']:.2%}",
        f"- Best name: {rb['best']}",
        f"- Worst name: {rb['worst']}",
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
        "## Recommendation for the P4 optimizer",
        "",
        recommendation,
        "",
    ]
    return "\n".join(lines)