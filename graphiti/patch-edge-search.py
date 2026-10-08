"""Build-time patch: edge search takes its endpoints from the relationship the index returned (2026-09-06).

Upstream getzep/graphiti PR #1500 (open since 2026-09-01; #1507 and #1287 tried the same and were closed):
FalkorDB edge search yields each matching relationship from the fulltext index or the BFS path and then
re-MATCHes it, `MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)`. The planner cannot anchor a
relationship by property inside a node-rel-node pattern, so every yielded row scans the graph: O(matches x
graph). Measured on the hub 2026-09-06 (2,467 entities, 3,569 edges): the fulltext call answers in 1 ms, the
re-MATCH takes 131 s, the query timeout is 120 s, and the episode is dropped after all of its LLM work
(<kb-entry> row 70). startNode(e)/endNode(e) on the yielded relationship give the same
endpoints in 3 ms. The shape lives in two files: the shared search_utils (the live path; driver.search_interface
is None by default) and the FalkorDB driver's search_ops (fulltext and BFS). Same replace-verify-ast-check
contract as patch-worker-ref.py; the paths can be overridden with GESTALT_SU_PATH / GESTALT_SO_PATH for the test.
"""
import ast
import glob
import os
import pathlib

SITE = "/app/mcp/.venv/lib/python3.*/site-packages/graphiti_core"


def _default(rel: str) -> str:
    hits = sorted(glob.glob(f"{SITE}/{rel}"))
    return hits[0] if hits else f"{SITE}/{rel}"


SU = pathlib.Path(os.environ.get("GESTALT_SU_PATH") or _default("search/search_utils.py"))
SO = pathlib.Path(os.environ.get("GESTALT_SO_PATH") or _default("driver/falkordb/operations/search_ops.py"))

FULLTEXT_OLD = "YIELD relationship AS rel, score\n{i}MATCH (n:Entity)-[e:RELATES_TO {{uuid: rel.uuid}}]->(m:Entity)\n"
FULLTEXT_NEW = "YIELD relationship AS e, score\n{i}WITH e, score, startNode(e) AS n, endNode(e) AS m\n"
# The BFS query is an f-string upstream, so its braces are doubled in the source text.
BFS_OLD = "MATCH (n:Entity)-[e:RELATES_TO {{uuid: rel.uuid}}]-(m:Entity)\n"
BFS_NEW = "WITH rel AS e, startNode(rel) AS n, endNode(rel) AS m\n{i}WHERE type(e) = 'RELATES_TO'\n{i}WITH e, n, m\n"


def replace_once(src: str, old: str, new: str, what: str) -> str:
    n = src.count(old)
    assert n == 1, f"{what} not found exactly once ({n}); upstream changed, re-derive the patch"
    return src.replace(old, new, 1)


su = SU.read_text()
su = replace_once(su, FULLTEXT_OLD.format(i=" " * 4), FULLTEXT_NEW.format(i=" " * 4), f"{SU.name} fulltext re-MATCH")
ast.parse(su)

so = SO.read_text()
so = replace_once(so, FULLTEXT_OLD.format(i=" " * 12), FULLTEXT_NEW.format(i=" " * 12), f"{SO.name} fulltext re-MATCH")
so = replace_once(so, BFS_OLD, BFS_NEW.format(i=" " * 12), f"{SO.name} BFS re-MATCH")
ast.parse(so)

SU.write_text(su)
SO.write_text(so)
print("patched", SU, SO)
