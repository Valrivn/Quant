"""
Compare sentiment labels from Gemini and BigPickle.
Computes agreement rate, per-class agreement, Cohen's kappa, and prints disagreement cases.
"""

import os
import sqlite3
import argparse
from typing import List, Dict

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DISTILLED_DB = os.path.join(ROOT_DIR, "data", "reddit_distilled.db")


def cohen_kappa(y1: List[int], y2: List[int]) -> float:
    """Compute Cohen's kappa coefficient manually."""
    if not y1 or not y2 or len(y1) != len(y2):
        return 0.0
    n = len(y1)
    classes = set(y1) | set(y2)
    
    # Observed agreement
    po = sum(1 for a, b in zip(y1, y2) if a == b) / n
    
    # Expected agreement
    pe = 0.0
    for c in classes:
        p1 = sum(1 for x in y1 if x == c) / n
        p2 = sum(1 for x in y2 if x == c) / n
        pe += p1 * p2
        
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1.0 - pe)


def main():
    parser = argparse.ArgumentParser(description="Compare Gemini vs BigPickle labelers")
    parser.add_argument("--db-path", type=str, default=DISTILLED_DB, help="Path to distilled sqlite database")
    parser.add_argument("--limit-disagreements", type=int, default=10, help="Max disagreement cases to print")
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"Database not found: {args.db_path}")
        return

    conn = sqlite3.connect(args.db_path)
    cursor = conn.cursor()

    # Query matching records
    query = """
        SELECT 
            g.id, 
            g.combined_text, 
            g.label AS gemini_label, 
            b.label AS bigpickle_label
        FROM reddit_labels g
        JOIN reddit_labels b ON g.id = b.id
        WHERE g.labeled_by = 'gemini' AND b.labeled_by = 'bigpickle'
    """
    
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
    except sqlite3.OperationalError as e:
        print(f"Error querying database: {e}")
        conn.close()
        return

    conn.close()

    if not rows:
        print("No cases found where both 'gemini' and 'bigpickle' labels exist.")
        return

    total = len(rows)
    gemini_labels = [row[2] for row in rows]
    bigpickle_labels = [row[3] for row in rows]

    # Calculate overall agreement
    agreed = sum(1 for g, b in zip(gemini_labels, bigpickle_labels) if g == b)
    overall_agreement = agreed / total if total > 0 else 0.0

    # Calculate per-class agreement
    class_names = {0: "Bearish (0)", 1: "Neutral (1)", 2: "Bullish (2)"}
    class_agreement = {}
    for c in [0, 1, 2]:
        # filter cases where at least one labeler assigned class c
        relevant = [row for row in rows if row[2] == c or row[3] == c]
        if not relevant:
            class_agreement[c] = (0, 0, 0.0) # (total, agreed, pct)
            continue
        c_agreed = sum(1 for row in relevant if row[2] == row[3])
        class_agreement[c] = (len(relevant), c_agreed, c_agreed / len(relevant))

    # Calculate Cohen's kappa
    kappa = cohen_kappa(gemini_labels, bigpickle_labels)

    # Print results
    print("=" * 60)
    print(" LABELER COMPARISON REPORT: Gemini vs BigPickle")
    print("=" * 60)
    print(f"Total matching posts compared: {total}")
    print(f"Overall Agreement Rate:      {overall_agreement:.2%} ({agreed}/{total})")
    print(f"Cohen's Kappa Coefficient:   {kappa:.4f}")
    print("-" * 60)
    print("Per-Class Agreement Rates (where at least one labeled the class):")
    for c, name in class_names.items():
        rel_tot, rel_agr, pct = class_agreement[c]
        print(f"  {name:<15}: {pct:.2%} ({rel_agr}/{rel_tot})")
    
    # Identify and report disagreements
    disagreements = [row for row in rows if row[2] != row[3]]
    print("-" * 60)
    print(f"Disagreement Cases ({len(disagreements)} total):")
    
    for i, row in enumerate(disagreements[:args.limit_disagreements]):
        post_id, text, g_lbl, b_lbl = row
        clean_text = text.replace('\n', ' ')[:100] + "..." if len(text) > 100 else text.replace('\n', ' ')
        print(f"\nDisagreement #{i+1}:")
        print(f"  Post ID:        {post_id}")
        print(f"  Gemini Label:   {class_names.get(g_lbl, g_lbl)}")
        print(f"  BigPickle Label: {class_names.get(b_lbl, b_lbl)}")
        print(f"  Text Snippet:   {clean_text}")

    if len(disagreements) > args.limit_disagreements:
        print(f"\n... and {len(disagreements) - args.limit_disagreements} more disagreement cases.")
    print("=" * 60)


if __name__ == "__main__":
    main()
