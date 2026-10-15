"""
mood.py - Reads the emotional temperature of the coverage.

Plain English: it scores each headline or post from -1 (very negative: fear, anger, threat)
to +1 (very positive), then averages them. A story whose mood drops fast is often one being
pushed with fear or outrage, which is worth a closer look.

Standard terms: sentiment analysis. It uses VADER, a well-known sentiment scorer built for
short social-media text. If VADER isn't installed, it falls back to a small built-in word list
(less accurate, but it still works).   Install VADER:  python -m pip install vaderSentiment
"""

import re

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
except Exception:  # not installed: use the fallback below
    _vader = None

NEG = set("""attack attacks threat threats fear fears scare scared panic crisis catastrophic catastrophe disaster
danger dangerous deadly kill kills killed war chaos collapse fraud fake scam scams lie lies lying hack hacked
breach warn warns warning alarm alarming outrage furious angry hate violent violence riot crash risk risks
terror terrorist doom dire grim worst destroy destroyed manipulate manipulation propaganda disinformation
misinformation deepfake deepfakes troll trolls bot bots exploit exploited victim victims ban banned""".split())
POS = set("""safe safety calm win wins success hope hopeful help helps protect protected secure progress
breakthrough agree agreement peace trust trusted improve improved benefit benefits good great""".split())


def score(text: str) -> float:
    if _vader is not None:
        return _vader.polarity_scores(text)["compound"]
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return 0.0
    raw = sum(w in POS for w in words) - sum(w in NEG for w in words)
    return max(-1.0, min(1.0, raw / 3))


def average(texts: list[str]):
    vals = [score(t) for t in texts if t]
    return round(sum(vals) / len(vals), 3) if vals else None


def engine() -> str:
    return "VADER" if _vader is not None else "basic word list"
