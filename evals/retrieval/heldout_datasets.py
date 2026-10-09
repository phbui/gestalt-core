"""Sealed held-out retrieval sets as ir_datasets-style objects: LitSearch, TechQA and LongMemEval-S.

The files live under GESTALT_HELDOUT_DIR (default <repo>/.heldout), one folder per set. `heldout.py` fetches, seals and opens them.
Each object has docs_iter(), queries_iter(), qrels_iter() and docs_count(). The record fields match what beir_bench reads:
Doc(doc_id, title, text), Query(query_id, text), Qrel(query_id, doc_id, relevance).

Reads are gated. A set can be read only when GESTALT_HELDOUT_OPEN names it, and `heldout.py open` prints that line.
To let beir_bench see the sets, add this one line after its imports:

    import heldout_datasets; heldout_datasets.register()

register() puts "litsearch", "techqa" and "longmemeval-s" in the ir_datasets registry. Each also answers to the "/test" suffix.

LongMemEval-S is not a one-corpus set. Each question has its own haystack of chat sessions. This module exposes it
through question_ids(), corpus_for(qid) and qrels_for(qid). beir_bench has no per-question loop, so the orchestrator must add one:
for each question, build the index from corpus_for(qid), retrieve the one query, and score against qrels_for(qid).
"""
from __future__ import annotations

import json
import os
import zipfile
from collections import namedtuple
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
Doc = namedtuple("Doc", "doc_id title text")
Query = namedtuple("Query", "query_id text")
Qrel = namedtuple("Qrel", "query_id doc_id relevance")

# Pinned sources. Each file is (hf repo, path in the repo, path under the set folder).
SPECS = {
    "litsearch": {
        "license": "MIT (mteb/LitSearchRetrieval card). princeton-nlp/LitSearch lists no licence, so the flags file is used for labels only.",
        "sources": [
            {"repo": "mteb/LitSearchRetrieval", "commit": "f9c8810dcec439c17a4a6ed18baec1922a49b0c4"},
            {"repo": "princeton-nlp/LitSearch", "commit": "9573fb284a1026c998df47024b888a163f0f0e25"},
        ],
        "files": [
            ("mteb/LitSearchRetrieval", "corpus/test-00000-of-00001.parquet", "corpus/test-00000-of-00001.parquet"),
            ("mteb/LitSearchRetrieval", "queries/test-00000-of-00001.parquet", "queries/test-00000-of-00001.parquet"),
            ("mteb/LitSearchRetrieval", "qrels/test-00000-of-00001.parquet", "qrels/test-00000-of-00001.parquet"),
            ("princeton-nlp/LitSearch", "query/full-00000-of-00001.parquet", "flags/query.parquet"),
        ],
    },
    "techqa": {
        "license": "Apache-2.0 (nvidia/TechQA-RAG-Eval card)",
        "sources": [{"repo": "nvidia/TechQA-RAG-Eval", "commit": "0b5bbc84b7f07d6d09d063130e90b716d8d4a32a"}],
        "files": [
            ("nvidia/TechQA-RAG-Eval", "corpus.zip", "corpus.zip"),
            ("nvidia/TechQA-RAG-Eval", "train.json", "train.json"),
        ],
    },
    "longmemeval-s": {
        "license": "MIT (xiaowu0162/longmemeval-cleaned card)",
        "sources": [{"repo": "xiaowu0162/longmemeval-cleaned", "commit": "98d7416c24c778c2fee6e6f3006e7a073259d48f"}],
        "files": [("xiaowu0162/longmemeval-cleaned", "longmemeval_s_cleaned.json", "longmemeval_s_cleaned.json")],
    },
}


def heldout_dir() -> Path:
    return Path(os.environ.get("GESTALT_HELDOUT_DIR") or REPO / ".heldout")


def _open_names() -> set[str]:
    return {n for n in os.environ.get("GESTALT_HELDOUT_OPEN", "").split(",") if n}


class _Set:
    name = ""
    metadata = None  # ir_datasets wraps any registered object without this attribute in a Dataset that needs .has()

    def __init__(self, root: Path | str | None = None, gated: bool = True):
        self.root = Path(root) if root else heldout_dir() / self.name
        self.gated = gated

    def _check(self) -> None:
        if self.gated and self.name not in _open_names():
            raise PermissionError(f"{self.name} is sealed. Run `heldout.py open {self.name} --reason ...` and export the line it prints.")

    def docs_count(self) -> int:
        return sum(1 for _ in self.docs_iter())

    def unanswerable_ids(self) -> list[str]:
        return []

    def all_query_ids(self) -> list[str]:
        """Every query id the set holds, answerable or not, sorted. The seal hashes this list."""
        return sorted({q.query_id for q in self.queries_iter()} | set(self.unanswerable_ids()))


class LitSearch(_Set):
    """LitSearch on the MTEB mirror. Doc ids look like d<corpusid>. query_flags() carries the expert-vs-LLM label."""
    name = "litsearch"

    def _rows(self, sub: str):
        import pyarrow.parquet as pq

        for f in sorted((self.root / sub).glob("*.parquet")):
            yield from pq.read_table(f).to_pylist()

    def docs_iter(self):
        self._check()
        for r in self._rows("corpus"):
            yield Doc(str(r.get("_id", r.get("id"))), r.get("title") or "", r.get("text") or "")

    def queries_iter(self):
        self._check()
        for r in self._rows("queries"):
            yield Query(str(r.get("_id", r.get("id"))), r["text"])

    def qrels_iter(self):
        self._check()
        for r in self._rows("qrels"):
            yield Qrel(str(r["query-id"]), str(r["corpus-id"]), int(r["score"]))

    def query_flags(self) -> dict[str, dict]:
        """query_id -> {origin: "expert" or "llm", query_set, specificity, quality}. The flag file is matched by query text.

        The query_set names come from the original release. manual_* sets are expert-written. inline_* sets are LLM-generated.
        """
        self._check()
        by_text = {}
        for r in self._rows("flags"):
            by_text[r["query"]] = {"origin": "expert" if r["query_set"].startswith("manual") else "llm",
                                   "query_set": r["query_set"], "specificity": r["specificity"], "quality": r["quality"]}
        return {q.query_id: by_text[q.text] for q in self.queries_iter() if q.text in by_text}


class TechQA(_Set):
    """TechQA technotes. Answerable items have gold technotes. The impossible items have none and are abstention probes."""
    name = "techqa"

    def _items(self) -> list[dict]:
        return json.loads((self.root / "train.json").read_text(encoding="utf-8"))

    @staticmethod
    def _impossible(item: dict) -> bool:
        v = item["is_impossible"]
        return v is True or str(v).strip().lower() == "true"

    def docs_iter(self):
        self._check()
        with zipfile.ZipFile(self.root / "corpus.zip") as z:
            for n in sorted(z.namelist()):
                if not n.endswith(".txt"):
                    continue
                raw = z.read(n).decode("utf-8", "replace")
                title, _, rest = raw.partition("\n\nText:\n")
                if not rest:
                    title, rest = "", raw
                yield Doc(Path(n).stem, title.removeprefix("Title: ").strip(), rest)

    def docs_count(self) -> int:
        self._check()
        with zipfile.ZipFile(self.root / "corpus.zip") as z:
            return sum(1 for n in z.namelist() if n.endswith(".txt"))

    def queries_iter(self):
        self._check()
        for it in self._items():
            if not self._impossible(it):
                yield Query(it["id"], it["question"])

    def qrels_iter(self):
        self._check()
        for it in self._items():
            if not self._impossible(it):
                for c in it["contexts"]:
                    yield Qrel(it["id"], Path(c["filename"]).stem, 1)

    def unanswerable_queries(self):
        self._check()
        for it in self._items():
            if self._impossible(it):
                yield Query(it["id"], it["question"])

    def unanswerable_ids(self) -> list[str]:
        self._check()
        return sorted(q.query_id for q in self.unanswerable_queries())


class LongMemEvalS(_Set):
    """LongMemEval-S cleaned. A doc is one chat session. Ids ending _abs are abstention items with no qrels."""
    name = "longmemeval-s"
    FILE = "longmemeval_s_cleaned.json"

    def _items(self) -> list[dict]:
        if not hasattr(self, "_cache"):
            self._cache = json.loads((self.root / self.FILE).read_text(encoding="utf-8"))
        return self._cache

    @staticmethod
    def _session_doc(sid: str, turns: list[dict]) -> Doc:
        return Doc(sid, "", "\n".join(f"{t['role']}: {t['content']}" for t in turns))

    def _answerable(self):
        return [it for it in self._items() if not it["question_id"].endswith("_abs")]

    def question_ids(self) -> list[str]:
        self._check()
        return [it["question_id"] for it in self._answerable()]

    def corpus_for(self, question_id: str):
        """The haystack of one question as Doc records."""
        self._check()
        it = next((i for i in self._items() if i["question_id"] == question_id), None)
        if it is None:
            raise ValueError(f"unknown question_id {question_id!r}")
        for sid, turns in zip(it["haystack_session_ids"], it["haystack_sessions"]):
            yield self._session_doc(sid, turns)

    def qrels_for(self, question_id: str) -> dict[str, int]:
        self._check()
        it = next((i for i in self._answerable() if i["question_id"] == question_id), None)
        if it is None:
            raise ValueError(f"unknown or unanswerable question_id {question_id!r}")
        return {sid: 1 for sid in it["answer_session_ids"]}

    def docs_iter(self):
        """Every distinct session across all haystacks. A convenience only. It is not the per-question protocol."""
        self._check()
        seen = set()
        for it in self._items():
            for sid, turns in zip(it["haystack_session_ids"], it["haystack_sessions"]):
                if sid not in seen:
                    seen.add(sid)
                    yield self._session_doc(sid, turns)

    def queries_iter(self):
        self._check()
        for it in self._answerable():
            yield Query(it["question_id"], it["question"])

    def qrels_iter(self):
        self._check()
        for it in self._answerable():
            for sid in it["answer_session_ids"]:
                yield Qrel(it["question_id"], sid, 1)

    def unanswerable_ids(self) -> list[str]:
        self._check()
        return sorted(it["question_id"] for it in self._items() if it["question_id"].endswith("_abs"))


CLASSES = {c.name: c for c in (LitSearch, TechQA, LongMemEvalS)}


def lookup(name: str, root: Path | str | None = None, gated: bool = True) -> _Set:
    """Return the dataset for a name. It accepts "heldout/<name>" and a trailing "/test"."""
    key = name.removeprefix("heldout/").removesuffix("/test")
    if key not in CLASSES:
        raise KeyError(f"unknown held-out set {name!r}. Known: {sorted(CLASSES)}")
    return CLASSES[key](root, gated)


class _Registered:
    """What the registry holds: a stand-in that builds the real set on every attribute access.

    So the directory comes from GESTALT_HELDOUT_DIR at use time, not at registration time. beir_bench registers the names
    when it is imported, and a later process or test that points the variable elsewhere still reads the right files."""

    metadata = None

    def __init__(self, key: str):
        self._key = key

    def __getattr__(self, attr: str):
        return getattr(lookup(self._key), attr)


def register() -> None:
    """Add the three sets to the ir_datasets registry, with and without the /test suffix. Safe to call twice.

    Without ir_datasets installed (a checkout without the bench requirements) this does nothing, so importing the harness
    for its pure functions and tests still works; the first real dataset read then raises the usual ModuleNotFoundError."""
    try:
        import ir_datasets
    except ModuleNotFoundError:
        return

    for key in CLASSES:
        for alias in (key, f"{key}/test"):
            try:
                ir_datasets.registry.register(alias, _Registered(key))
            except RuntimeError:  # already registered by an earlier call
                pass
