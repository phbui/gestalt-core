#!/usr/bin/env python3
"""The sync ledger, split out of the shell script's heredocs (roadmap L6, 2026-10-06).

Shape, unchanged so old state files load: {slug: sha1-of-file, "#chunks": {chunk-name: sha1-of-chunk-content}}.
One key is new and optional: "#landed" {chunk-name: sha1}. "#chunks" says a chunk was ACKNOWLEDGED by Graphiti's
POST (queued). "#landed" says the graph was later SEEN to hold an Episodic node of that name. A chunk is pending
when "#chunks" holds a hash that "#landed" does not. The 2026-10-06 reboot lost 55 acknowledged chunks from
Graphiti's in-memory queue while the ledger still said sent, which is the gap this closes.

The shell script holds an flock on $STATE_DIR/graphiti-sync.lock for the whole run. This module never takes
that lock itself. Every write is a temp file in the same directory followed by os.replace.

Commands (python3 -m graphiti_sync.ledger <cmd>, run with tools/ on PYTHONPATH):
  check STATE                       exit 0 when readable or missing, exit 4 with a message when corrupt
  plan STATE KNOWLEDGE SLUG FORCE   print "slug<TAB>sha" per entry that passes the file gate
  chunkstate STATE                  print the "#chunks" map as JSON
  record STATE MANIFEST             read one payload JSON on stdin, record its chunk (the per-chunk ack write)
  commit STATE SYNCED CHUNKS        merge slug hashes and chunk hashes from two TSV files
  mark-synced STATE MANIFEST NAMES  record the planned hash of NAMES without sending
  reconcile STATE [--dry-run] [--slug S ...]   ask the graph which pending chunks landed; print the missing ones
"""
import hashlib
import json
import os
import shlex
import subprocess
import sys

EXIT_CORRUPT = 4
EXIT_UNREACHABLE = 5


class LedgerError(Exception):
    pass


class GraphError(LedgerError):
    """The graph could not be queried from this node. The ledger is fine and must stay untouched."""


def load(path):
    """A missing file is a normal first run. Anything else that is not a JSON object is a hard error."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise LedgerError("ledger %s is unreadable: %s" % (path, e))
    try:
        state = json.loads(raw)
    except ValueError as e:
        raise LedgerError("ledger %s is not valid JSON (%s). Nothing was sent. Restore it from a backup or move it aside "
                          "to start a first run." % (path, e))
    if not isinstance(state, dict):
        raise LedgerError("ledger %s is not a JSON object. Nothing was sent." % path)
    for k in ("#chunks", "#landed"):
        if k in state and not isinstance(state[k], dict):
            raise LedgerError("ledger %s has a malformed %s key. Nothing was sent." % (path, k))
    return state


def save(path, state):
    """Atomic: the old file stays intact until os.replace succeeds."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_manifest(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.rstrip("\n")
            if line:
                n, h = line.split("\t", 1)
                out[n] = h
    except FileNotFoundError:
        pass
    return out


def plan(state, knowledge_dir, slug_arg="", force_all=False):
    if not os.path.isdir(knowledge_dir):
        return []
    files = sorted(f for f in os.listdir(knowledge_dir) if f.endswith(".md"))
    if slug_arg:
        files = [f for f in files if f == slug_arg + ".md"]
    out = []
    for fname in files:
        slug = fname[:-3]
        with open(os.path.join(knowledge_dir, fname), "rb") as fh:
            sha = hashlib.sha1(fh.read()).hexdigest()
        if slug_arg or force_all or state.get(slug) != sha:
            out.append((slug, sha))
    return out


def record_chunk(path, name, sha):
    """Called at the moment a chunk's POST is acknowledged (B-04), so a later failure never re-sends it."""
    state = load(path)
    state.setdefault("#chunks", {})[name] = sha
    save(path, state)


def commit(path, synced_lines, chunk_lines):
    state = load(path)
    for line in synced_lines:
        line = line.rstrip("\n")
        if line:
            slug, sha = line.split("\t", 1)
            state[slug] = sha
    chunks = state.setdefault("#chunks", {})
    for line in chunk_lines:
        line = line.rstrip("\n")
        if line:
            n, h = line.split("\t", 1)
            chunks[n] = h
    save(path, state)


def pending(state, slugs=()):
    """name -> sha for chunks acknowledged but not seen in the graph, optionally limited to entries."""
    landed = state.get("#landed", {})
    out = {}
    for n, h in state.get("#chunks", {}).items():
        if landed.get(n) == h:
            continue
        if slugs:
            base = n.split("#", 1)[0]
            if base not in {"kb-" + s for s in slugs}:
                continue
        out[n] = h
    return out


def graph_names(names, cli=None, graph=None):
    """The subset of NAMES that exist as Episodic nodes. Raises LedgerError when the graph cannot be queried."""
    cli = shlex.split(cli if cli is not None else os.environ.get("GESTALT_FALKORDB_CLI", "docker exec gestalt-falkordb redis-cli"))
    graph = graph or os.environ.get("GESTALT_GRAPH", "gestalt")
    if not cli:
        raise GraphError("GESTALT_FALKORDB_CLI is empty")
    found, names = set(), sorted(names)
    for i in range(0, len(names), 100):
        batch = names[i:i + 100]
        q = "MATCH (e:Episodic) WHERE e.name IN %s RETURN DISTINCT e.name" % json.dumps(batch)
        try:
            tail = ["--json", "GRAPH.RO_QUERY", graph, q]  # a read: reconcile never writes to the graph
            # Over ssh the remote shell parses the command line again, so the Cypher's parentheses and quotes
            # must be quoted for it (found 2026-10-06: the documented ssh form died with a bash syntax error).
            argv = [*cli, *(shlex.quote(t) for t in tail)] if os.path.basename(cli[0]) == "ssh" else [*cli, *tail]
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise GraphError("graph command exited %d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[:200]))
            rows = json.loads(r.stdout)[1]
            found.update(row[0] for row in rows if row)
        except GraphError:
            raise
        except Exception as e:
            raise GraphError("graph query failed: %s" % e)
    return found


def reconcile(path, slugs=(), dry_run=False, cli=None):
    """Returns (landed_names, missing_names). Changes the ledger only when the graph answered and not dry_run."""
    state = load(path)
    pend = pending(state, slugs)
    if not pend:
        return [], []
    present = graph_names(pend, cli)
    landed = sorted(n for n in pend if n in present)
    missing = sorted(n for n in pend if n not in present)
    if landed and not dry_run:
        state.setdefault("#landed", {}).update({n: pend[n] for n in landed})
        save(path, state)
    return landed, missing


def _main(argv):
    cmd, a = argv[0], argv[1:]
    try:
        if cmd == "check":
            load(a[0])
        elif cmd == "plan":
            for slug, sha in plan(load(a[0]), a[1], a[2], a[3] == "1"):
                print("%s\t%s" % (slug, sha))
        elif cmd == "chunkstate":
            print(json.dumps(load(a[0]).get("#chunks", {})))
        elif cmd == "record":
            name = json.loads(sys.stdin.read())["params"]["arguments"]["name"]
            sha = read_manifest(a[1]).get(name)
            if sha:
                record_chunk(a[0], name, sha)
        elif cmd == "commit":
            commit(a[0], open(a[1]).readlines() if os.path.exists(a[1]) else [], open(a[2]).readlines() if os.path.exists(a[2]) else [])
        elif cmd == "mark-synced":
            planned = read_manifest(a[1])
            want = [n for n in a[2].split(",") if n]
            gone = [n for n in want if n not in planned]
            if gone:
                print("mark-synced: not in the current plan, nothing written: " + ", ".join(gone), file=sys.stderr)
                return 3
            state = load(a[0])
            chunks = state.setdefault("#chunks", {})
            for n in want:
                print("mark-synced\t%s\t%s -> %s" % (n, chunks.get(n, "-")[:12], planned[n][:12]), file=sys.stderr)
                chunks[n] = planned[n]
            save(a[0], state)
            print("mark-synced: %d chunk hash(es) written to the ledger, nothing sent" % len(want), file=sys.stderr)
        elif cmd == "reconcile":
            dry = "--dry-run" in a
            slugs = [x for x in a[1:] if not x.startswith("--")]
            landed, missing = reconcile(a[0], slugs, dry)
            for n in landed:
                print("landed\t%s" % n)
            for n in missing:
                print("missing\t%s" % n)
            print("reconcile: %d landed%s, %d missing" % (len(landed), " (not written, dry run)" if dry and landed else "", len(missing)), file=sys.stderr)
        else:
            print("unknown command " + cmd, file=sys.stderr)
            return 2
    except LedgerError as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return EXIT_UNREACHABLE if isinstance(e, GraphError) else EXIT_CORRUPT
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
