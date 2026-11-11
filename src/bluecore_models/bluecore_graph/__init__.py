"""
Saving a BIBFRAME graph as Blue Core records.

A payload, such as an LC record from a bulk load or a Work an editor saves,
describes several resources: Hubs, Works, Instances, and the Other Resources
they cite (subjects, agents, vocabulary terms). save_graph() and
BluecoreGraph(graph).save() turn it into rows: one per Hub, Work, Instance and
Other Resource, and the links between them. A save goes in three steps, each
doing one kind of work.

**Plan** (SavePlan, in save.py). The payload is copied, so the caller's graph
never changes, and normalised. Then every resource gets a Role, from the payload
and the save's mode alone, without the database.

**Resolve** (SavePlan.resolve, giving a Resolution). For each Hub, Work and
Instance, the save reuses the Bluecore URI an earlier save gave the same source
(found through bf:derivedFrom), or mints one. Then it fetches the rows we hold
for each resource and decides each one's Action. It reads the database and
writes nothing. It works on its own copy of the plan's graph, because minting
renames resources: the plan stays as it was, so it can be resolved again, on a
retry or as a dry run that's thrown away. Resolution.render() shows the result,
one line per resource.

**Apply** (SavePlan.apply). In each attempt of the retry loop, the save resolves
afresh on that attempt's session, because another save may have committed the
same resource meanwhile. Then it writes and links exactly what that resolution
says. Nothing here decides policy.

A dry run is what a save would do at that moment, not a promise. Applying
resolves again, so a new resource gets a different UUID from the one a dry run
showed, and a save that commits in between can change an action.

## The policy

What a save does is decided in policy.py, by three answers:

- **Role**: what a resource is in this save (_classify).
- **Action**: what the save does to its row, from its role and the row we hold
  (_action).
- **Citation**: what each URI a Work or Instance cites is, which decides whether
  it is stored as an Other Resource and whether the Work or Instance is linked
  to it (_citation).

docs/save-policy.md states these as rules and cases, in words, for catalogers as
well as developers. Every case is a test (tests/policy_cases.py), and each case's
"In the code" line names the Kind, Role and Action, or the Citation, it comes to.

## Roles

A role says what a resource is in this particular save. It is decided per
resource and kind: a URI typed both bf:Work and bf:Instance gets two roles.

**PRIMARY**: what this save is authoritative for. Its description is written:
inserted if new, otherwise the row we hold is updated to match.

- In a bulk load (Ingest), every described Hub, Work and Instance. Other
  Resources count only when the load is told to update them
  (update_other_resources).
- In an API save (Write), only resources of the kind being written, such as the
  Work an editor saves, including the new resource the save is creating.
- In an Ingest, a resource we already hold in full under its source's name is
  kept, not overwritten, so loading LC data again doesn't wipe out a cataloger's
  edits.

**REFERENCE**: something the save mentions but isn't in charge of. It is created
if we don't hold it. If we do, it is left alone, including its links.

- In a Write, every resource of another kind: saving a Work that points at an
  Instance mustn't rewrite that Instance.
- In an Ingest, Other Resources, when the load isn't updating them.

**STUB**: a placeholder, in an Ingest only. It is a Hub, Work or Instance the
record names but doesn't describe: the record uses adminMetadata, but not for
this resource. It is created if absent, marked incomplete (mstatus/incmp), and
kept as it is if we hold it. When a later load describes it, it arrives as
PRIMARY and replaces the stub.

**INLINE**: an anonymous part of another record, a blank node such as a Work
described inside an Instance. It never gets a row of its own; its description
stays in its parent's. The one blank node that isn't INLINE is the new resource
a Write is creating.

| Role      | Not held yet              | Already held                           |
| --------- | ------------------------- | -------------------------------------- |
| PRIMARY   | INSERT                    | UPDATE (an Ingest keeps one held in full) |
| REFERENCE | INSERT                    | KEEP                                   |
| STUB      | INSERT, marked incomplete | KEEP                                   |
| INLINE    | no row                    | no row                                 |
"""

from bluecore_models.bluecore_graph.policy import (
    STUB_STATUS,
    Action,
    Citation,
    Entry,
    Ingest,
    Kind,
    Mode,
    Role,
    Write,
    _action,
    _admin_nodes,
    _citation,
    _classify,
    _mode,
    _stored_is_stub,
)
from bluecore_models.bluecore_graph.save import (
    DEFAULT_STATUS,
    SAVE_MAX_ATTEMPTS,
    BluecoreGraph,
    BluecoreGraphError,
    Resolution,
    SavePlan,
    _copy_graph,
    _remove_admin_metadata,
    _remove_triples_by_type,
    save_graph,
)

__all__ = [
    "DEFAULT_STATUS",
    "SAVE_MAX_ATTEMPTS",
    "STUB_STATUS",
    "Action",
    "BluecoreGraph",
    "BluecoreGraphError",
    "Citation",
    "Entry",
    "Ingest",
    "Kind",
    "Mode",
    "Resolution",
    "Role",
    "SavePlan",
    "Write",
    "_action",
    "_admin_nodes",
    "_citation",
    "_classify",
    "_copy_graph",
    "_mode",
    "_remove_admin_metadata",
    "_remove_triples_by_type",
    "_stored_is_stub",
    "save_graph",
]
