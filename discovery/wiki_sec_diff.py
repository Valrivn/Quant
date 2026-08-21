"""Wiki-SEC Alignment and Novelty Diff (B-20260820-001 ruling).

Compares names found on Wikidata with SEC names to measure overlap and novelty.
"""

from typing import Dict, List, Set


def diff_wiki_sec(wiki_names: Set[str], sec_names: Set[str]) -> dict:
    """Return three-bucket diff analysis comparing Wikidata names with SEC names."""
    sec_only = sorted(list(sec_names - wiki_names))
    wiki_only = sorted(list(wiki_names - sec_names))
    both = sorted(list(wiki_names & sec_names))

    summary = {
        "wiki_count": len(wiki_names),
        "sec_count": len(sec_names),
        "overlap_count": len(both),
        "wiki_only_count": len(wiki_only),
    }

    return {
        "sec_only": sec_only,
        "wiki_only": wiki_only,
        "both": both,
        "summary": summary,
    }


def passes_bar(diff: dict, min_novel: int, min_corroborated: int) -> bool:
    """Check if novelty and overlap metrics meet minimum acceptable thresholds."""
    wiki_only = diff.get("wiki_only", [])
    both = diff.get("both", [])
    return len(wiki_only) >= min_novel and len(both) >= min_corroborated
