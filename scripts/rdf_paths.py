"""Summarise the ways one kind of resource reaches another in RDF data.

Answers questions of the form "how many ways is a Work connected to a Hub, and how
often is each one used?". Direct predicates are only part of the story -- BIBFRAME
often reaches a resource through an intervening node, as bf:relation does via
bf:Relation -- so this walks paths rather than single triples and reports each
distinct route with a count.

    # every route from a Work to a Hub in a batch
    rdf_paths.py sample/batch.jsonld

    # the same over archived CBDs, stopping after 2000 documents
    rdf_paths.py uploads/batch_00023.tar.gz --limit 2000

    # some other pair of classes
    rdf_paths.py sample/batch.jsonld --from bf:Instance --to bf:Item

Each document in an archive is parsed as its own graph, so a resource never reaches
one that arrived in a different file -- merging them would invent routes that exist in
no real payload.
"""

import argparse
import collections
import glob
import json
import sys
import tarfile
import warnings

warnings.filterwarnings("ignore")

import rdflib
from rdflib import BNode, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS

from bluecore_models.namespaces import BF, BFLC, MADS
from bluecore_models.utils.graph import load_jsonld

PREFIXES = {
    "bf": BF,
    "bflc": BFLC,
    "mads": MADS,
    "rdf": RDF,
    "rdfs": RDFS,
    "dcterms": DCTERMS,
}


def term(value: str) -> URIRef:
    """Resolve a CURIE like bf:Hub, or pass a full IRI through."""
    if "://" in value:
        return URIRef(value)
    prefix, _, name = value.partition(":")
    if prefix not in PREFIXES:
        raise argparse.ArgumentTypeError(
            f"unknown prefix {prefix!r}; known: {', '.join(sorted(PREFIXES))}"
        )
    return URIRef(str(PREFIXES[prefix]) + name)


def short(value) -> str:
    text = str(value)
    for separator in ("#", "/"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    return text


class Summary:
    """Accumulates routes across any number of documents."""

    def __init__(self, args):
        self.args = args
        self.routes: collections.Counter = collections.Counter()
        self.reverse_routes: collections.Counter = collections.Counter()
        self.pairs: dict = collections.defaultdict(set)
        self.annotations: collections.Counter = collections.Counter()
        self.unreached: collections.Counter = collections.Counter()
        self.documents = self.documents_matching = 0
        self.sources = self.targets = 0

    def label(self, graph, node, sources, targets) -> str:
        if node in targets:
            return short(self.args.to_class)
        if node in sources:
            return short(self.args.from_class)
        types = sorted(
            short(t) for t in graph.objects(node, RDF.type) if isinstance(t, URIRef)
        )
        kind = "/".join(types) or ("bnode" if isinstance(node, BNode) else "uri")
        return f"[{kind}]"

    def routes_from(self, graph, start, targets):
        """Predicate routes from start to any target, breadth first."""
        found = []
        queue = collections.deque([(start, (), {start})])
        while queue:
            node, route, seen = queue.popleft()
            if len(route) >= self.args.depth:
                continue
            for predicate, obj in graph.predicate_objects(node):
                if obj in seen or not isinstance(obj, (URIRef, BNode)):
                    continue
                step = route + ((predicate, obj),)
                if obj in targets:
                    found.append(step)  # a target is a destination; don't walk past it
                    continue
                queue.append((obj, step, seen | {obj}))
        return found

    def walk(self, graph, starts, ends, sources, targets, into) -> tuple[set, set]:
        """Record routes from starts to ends, returning (ends hit, starts that hit).

        Both are needed because the caller cares about which *targets* are connected,
        and a target is an end when walking forwards but a start when walking back.
        """
        reached = set()
        departed = set()
        for start in starts:
            for route in self.routes_from(graph, start, ends):
                departed.add(start)
                rendered = self.label(graph, start, sources, targets)
                for predicate, obj in route:
                    rendered += (
                        f" --{short(predicate)}--> "
                        f"{self.label(graph, obj, sources, targets)}"
                    )
                into[rendered] += 1
                self.pairs[rendered].add((str(start), str(route[-1][1])))
                reached.add(route[-1][1])
                # note any annotating property on the intermediate nodes; for a
                # bf:Relation that is bf:relationship, which says what kind it is
                for _, node in route[:-1]:
                    for predicate in self.args.annotate:
                        for value in graph.objects(node, predicate):
                            self.annotations[f"{short(predicate)}={short(value)}"] += 1
        return reached, departed

    def add(self, graph) -> None:
        self.documents += 1
        targets = set(graph.subjects(RDF.type, self.args.to_class))
        if not targets:
            return
        # a resource typed as both counts as a target, not a source: LC types Hubs as
        # bf:Work as well, and BluecoreGraph.works() excludes them for the same reason
        sources = {
            s
            for s in graph.subjects(RDF.type, self.args.from_class)
            if s not in targets
        }
        if not sources:
            return
        self.documents_matching += 1
        self.sources += len(sources)
        self.targets += len(targets)

        # forwards, a connected target is one we reached
        reached, _ = self.walk(graph, sources, targets, sources, targets, self.routes)

        # The same relationship may be stated from either end -- a Hub document says
        # bf:hasExpression where a Work says bf:expressionOf -- and following only
        # outgoing edges from the source would miss that entirely. So walk the other
        # way too, reported separately: a route in the other direction is a different
        # statement, not the same one seen twice. Backwards, a connected target is one
        # a route departed from rather than one we arrived at.
        _, departed = self.walk(
            graph, targets, sources, sources, targets, self.reverse_routes
        )

        for missed in targets - reached - departed:
            referenced_by = {short(p) for _, p in graph.subject_predicates(missed)}
            self.unreached[", ".join(sorted(referenced_by)) or "(nothing)"] += 1

    def report(self) -> None:
        source_name = short(self.args.from_class)
        target_name = short(self.args.to_class)
        print(
            f"{self.documents:,} documents "
            f"({self.documents_matching:,} with both a {source_name} and a {target_name}) | "
            f"{self.sources:,} {source_name}s and {self.targets:,} {target_name}s in those\n"
        )
        if not self.routes and not self.reverse_routes:
            print(f"no route found between {source_name} and {target_name}")
            return
        for heading, routes in (
            (f"routes from a {source_name} to a {target_name}:", self.routes),
            (f"routes from a {target_name} to a {source_name}:", self.reverse_routes),
        ):
            if not routes:
                continue
            print(heading)
            for rendered, count in routes.most_common():
                print(
                    f"  {count:6} traversals, "
                    f"{len(self.pairs[rendered]):6} distinct pairs"
                )
                print(f"         {rendered}")
            print()
        if self.annotations:
            print("\nproperties on intermediate nodes:")
            for annotation, count in self.annotations.most_common(15):
                print(f"  {count:6}  {annotation}")
        if self.unreached:
            print(
                f"\n{target_name}s no {source_name} reaches, by what references them:"
            )
            for referenced_by, count in self.unreached.most_common(8):
                print(f"  {count:6}  {referenced_by}")


def read_file(path):
    """Parse one file, whatever serialization it is in.

    load_jsonld is used for JSON-LD rather than rdflib's parser because a batch is
    often a list of node objects, which rdflib will not take directly. Anything else
    is left to rdflib to sniff from the extension.
    """
    if path.endswith((".jsonld", ".json")):
        return load_jsonld(json.load(open(path)))
    graph = rdflib.Graph()
    graph.parse(path)
    return graph


def documents(paths, limit):
    """Yield a graph per input document, from files or tar archives."""
    count = 0
    for pattern in paths:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            if path.endswith((".tar.gz", ".tgz")):
                with tarfile.open(path, "r:gz") as archive:
                    for member in archive:
                        if not (member.isfile() and member.name.endswith(".rdf")):
                            continue
                        graph = rdflib.Graph()
                        try:
                            graph.parse(
                                data=archive.extractfile(member).read().decode(),
                                format="xml",
                            )
                        except Exception as error:  # noqa: BLE001
                            # one unparseable file shouldn't abort a whole scan
                            print(f"skipped {member.name}: {error}", file=sys.stderr)
                            continue
                        yield graph
                        count += 1
                        if limit and count >= limit:
                            return
            else:
                yield read_file(path)
                count += 1
                if limit and count >= limit:
                    return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="+", help="JSON-LD files or .tar.gz archives")
    parser.add_argument(
        "--from",
        dest="from_class",
        type=term,
        default=term("bf:Work"),
        help="class to start from (default bf:Work)",
    )
    parser.add_argument(
        "--to",
        dest="to_class",
        type=term,
        default=term("bf:Hub"),
        help="class to look for (default bf:Hub)",
    )
    parser.add_argument(
        "--depth", type=int, default=4, help="longest route to consider (default 4)"
    )
    parser.add_argument(
        "--annotate",
        type=term,
        action="append",
        help="property of intermediate nodes to report (default bf:relationship)",
    )
    parser.add_argument("--limit", type=int, help="stop after this many documents")
    args = parser.parse_args(argv)
    if args.annotate is None:
        args.annotate = [term("bf:relationship")]

    summary = Summary(args)
    for graph in documents(args.paths, args.limit):
        summary.add(graph)
    summary.report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
