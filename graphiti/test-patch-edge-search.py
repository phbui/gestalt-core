#!/usr/bin/env python3
"""patch-edge-search.py discriminates: applied to the upstream shapes (graphiti-core 0.29.3, both files) it
replaces the three per-row re-MATCH sites with startNode/endNode; applied to a file without a shape it refuses
rather than silently doing nothing. The fulltext rewrite is the exact query that answered in 3.2 ms on the hub
where the re-MATCH took 131 s (2026-09-06)."""
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PATCH = HERE / "patch-edge-search.py"

UPSTREAM_SU = '''async def edge_fulltext_search(driver, query):
    match_query = """
    YIELD relationship AS rel, score
    MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)
    """
    if driver.provider == "KUZU":
        match_query = """
        YIELD node, score
        MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_ {uuid: node.uuid})-[:RELATES_TO]->(m:Entity)
        """
    return match_query
'''

UPSTREAM_SO = '''async def edge_fulltext_search(self, limit):
    cypher = (
        "CALL db.idx.fulltext.queryRelationships('edge_name_and_fact', $query)"
        + """
            YIELD relationship AS rel, score
            MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)
            """
    )
    return cypher


async def edge_bfs_search(self, max_depth):
    cypher = (
        f"""
            UNWIND $bfs_origin_node_uuids AS origin_uuid
            MATCH path = (origin {{uuid: origin_uuid}})-[:RELATES_TO|MENTIONS*1..{max_depth}]->(:Entity)
            UNWIND relationships(path) AS rel
            MATCH (n:Entity)-[e:RELATES_TO {{uuid: rel.uuid}}]-(m:Entity)
            """
    )
    return cypher
'''


def run(su_text: str, so_text: str):
    with tempfile.TemporaryDirectory() as d:
        su = pathlib.Path(d) / "search_utils.py"
        so = pathlib.Path(d) / "search_ops.py"
        su.write_text(su_text)
        so.write_text(so_text)
        env = dict(os.environ, GESTALT_SU_PATH=str(su), GESTALT_SO_PATH=str(so))
        r = subprocess.run([sys.executable, str(PATCH)], env=env, capture_output=True, text=True)
        return r.returncode, su.read_text(), so.read_text(), r.stderr


rc, su, so, err = run(UPSTREAM_SU, UPSTREAM_SO)
assert rc == 0, err
assert "    YIELD relationship AS e, score\n    WITH e, score, startNode(e) AS n, endNode(e) AS m\n" in su
assert "uuid: rel.uuid" not in su
assert "RelatesToNode_ {uuid: node.uuid}" in su, "the Kuzu branch is not ours to touch"
assert "            YIELD relationship AS e, score\n            WITH e, score, startNode(e) AS n, endNode(e) AS m\n" in so
assert "            WITH rel AS e, startNode(rel) AS n, endNode(rel) AS m\n            WHERE type(e) = 'RELATES_TO'\n            WITH e, n, m\n" in so
assert "uuid: rel.uuid" not in so
assert "{{uuid: origin_uuid}}" in so, "the path anchor keeps its doubled braces"
compile(su, "search_utils.py", "exec")
compile(so, "search_ops.py", "exec")

# Applying twice must refuse: the shape is gone after the first pass.
rc2, _, _, err2 = run(su, so)
assert rc2 != 0 and "not found exactly once" in err2, "patch must refuse an already-patched file"

rc3, _, _, err3 = run(UPSTREAM_SU, UPSTREAM_SO.replace("{{uuid: rel.uuid}}]-(m:Entity)", "{{uuid: rel.uuid}}]->(m:Entity)"))
assert rc3 != 0 and "BFS re-MATCH not found" in err3, "patch must refuse when one site changed upstream"
print("ok: patch-edge-search applies to the upstream shapes and refuses any other")
