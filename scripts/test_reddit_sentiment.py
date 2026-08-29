"""
Smoke tests for RedditSentimentEngine -- 30 hand-crafted WSB posts.
Target: >=70% accuracy (>=21/30 correct).
"""

import sys
import os
import io

# Fix Windows console encoding for emoji output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Ensure repo root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the module directly to avoid heavy __init__.py chain in
# Qualitative/psychological/__init__.py which requires the package
# to be installed as 'psychological'.
import importlib.util
_mod_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Qualitative", "psychological", "sentiment_training", "reddit_sentiment.py",
)
_spec = importlib.util.spec_from_file_location("reddit_sentiment", _mod_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
RedditSentimentEngine = _mod.RedditSentimentEngine


# -- Test cases ------------------------------------------------------------
# (text, expected_label)
# label: 0=bearish, 1=neutral, 2=bullish

BULLISH_POSTS = [
    ("YOLO'd my life savings into GME calls rocket moon tendies", 2),
    ("Diamond hands HOLD THE LINE boys", 2),
    ("to the moon TSLA calls printing money rocket", 2),
    ("Bullish on NVDA undervalued buying the dip", 2),
    ("AMC gang rise up diamond hands tendies moon", 2),
    ("SPY calls to the moon tendies printing green day", 2),
    ("YOLO'd into AMD calls diamond hands rocket", 2),
    ("Bull gang assemble AAPL calls printing tendies", 2),
    ("diamond hands GME moon tendies rocket", 2),
    ("Tendies incoming Tesla calls printing money", 2),
]

BEARISH_POSTS = [
    ("GUH just lost everything on puts paper hands moment", 0),
    ("Paper hands sold the bottom bagholder life", 0),
    ("Blood red dump everything rekt", 0),
    ("Bear gang assemble puts printing crash dump", 0),
    ("Sell everything before the crash rekt", 0),
    ("Bagholder life GME crash dump blood red", 0),
    ("GUH paper hands moment lost all tendies", 0),
    ("Bear gang puts printing crash", 0),
    ("Dump dump dump rekt paper hands bagholder", 0),
    ("Blood red crash sell everything rekt", 0),
]

NEUTRAL_POSTS = [
    ("What's everyone's opinion on AAPL earnings?", 1),
    ("DD: NVDA revenue growth over 5 years analysis", 1),
    ("How do you evaluate a stock's intrinsic value?", 1),
    ("Question about index fund allocation strategy", 1),
    ("Comparing P/E ratios across semiconductor sector", 1),
    ("What broker do you use for options trading?", 1),
    ("Looking at the Fed's next rate decision", 1),
    ("Anyone tracking CPI data release this week?", 1),
    ("Discussion: Tesla vs Rivian market positioning", 1),
    ("Balanced portfolio allocation 60/40 still work?", 1),
]

ALL_POSTS = BULLISH_POSTS + BEARISH_POSTS + NEUTRAL_POSTS


def run_tests():
    engine = RedditSentimentEngine()
    results = engine.score_batch([t for t, _ in ALL_POSTS])
    labels = [e for _, e in ALL_POSTS]

    correct = 0
    bull_correct = 0
    bear_correct = 0
    neut_correct = 0
    errors = []

    for i, (result, expected) in enumerate(zip(results, labels)):
        got = result["label"]
        ok = got == expected
        if ok:
            correct += 1
            if expected == 2:
                bull_correct += 1
            elif expected == 0:
                bear_correct += 1
            else:
                neut_correct += 1
        else:
            category = "bullish" if i < 10 else ("bearish" if i < 20 else "neutral")
            errors.append(
                f"  [{category}] #{i+1} compound={result['compound']:.3f} "
                f"expected={expected} got={got} | {ALL_POSTS[i][0][:60]}..."
            )

    total = len(ALL_POSTS)
    acc = correct / total * 100

    print(f"\n{'='*60}")
    print(f"RedditSentimentEngine -- Test Results")
    print(f"{'='*60}")
    print(f"  Overall : {correct}/{total} correct  ({acc:.1f}%)")
    print(f"  Bullish : {bull_correct}/10  ({bull_correct*10}%)")
    print(f"  Bearish : {bear_correct}/10  ({bear_correct*10}%)")
    print(f"  Neutral : {neut_correct}/10  ({neut_correct*10}%)")
    print()

    if errors:
        print("FAILURES:")
        for e in errors:
            print(e)
        print()

    # Print per-case detail
    print("CASE DETAILS:")
    print(f"{'#':>3} {'Exp':>3} {'Got':>3} {'Comp':>7} {'Conf':<6} {'Slang':>5} {'Emoji':>5}  Text")
    print("-" * 90)
    for i, (result, expected) in enumerate(zip(results, labels)):
        print(
            f"{i+1:3d} {expected:3d} {result['label']:3d} {result['compound']:7.3f} "
            f"{result['confidence']:<6} {len(result['slang_hits']):5d} "
            f"{len(result['emoji_hits']):5d}  {ALL_POSTS[i][0][:50]}"
        )

    # Assert threshold
    if acc >= 70.0:
        print(f"\nPASS -- {acc:.1f}% accuracy meets 70% threshold")
    else:
        print(f"\nFAIL -- {acc:.1f}% accuracy below 70% threshold")
        sys.exit(1)


if __name__ == "__main__":
    # -v flag for verbose (already default verbose; kept for CLI compat)
    run_tests()
