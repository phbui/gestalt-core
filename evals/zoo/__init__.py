"""The retrieval zoo: one paired engine for many retrieval systems on the same BEIR sets.

Importing this package puts evals/retrieval and tools on sys.path, as the scripts there do for each other.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
for _p in (REPO / "evals" / "retrieval", REPO / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
