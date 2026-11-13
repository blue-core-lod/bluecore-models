# Evaluating BluecoreGraph as an abstraction

Reviewed October 31, 2026 · [blue-core-lod/bluecore-models](https://github.com/blue-core-lod/bluecore-models)

> **This is the analysis from before the refactor (#213).** For how the code works now, see the `bluecore_models.bluecore_graph` package docstring and [`docs/save-policy.md`](save-policy.md).

> **How to read this, as of 6 November 2026.** This is the analysis the refactor started from. It was written away from the code, and the plan in [#213](https://github.com/blue-core-lod/bluecore-models/issues/213) corrects parts of it after checking against `bluecore_graph.py` and its callers. **Where the two disagree, #213 is right.** Passages that #213 corrects are marked *Update* below. Use #213 for the phases, the baseline commit, line numbers and the current API sketch, and this document for the reasoning and background.

## Verdict

BluecoreGraph explains its intent very well in prose and poorly in structure. The comments carry real domain reasoning (LC typing Hubs as Works, the LDP empty `@id`, the `mstatus/incmp` collision with MARC 505), and `save()` is operationally solid. But the class has grown from a graph partitioner into a write-policy engine, and its central concepts — what role each resource plays in a payload, and what kind of save this is — live only in mutable flags and a dozen small predicates. A reader learns the model from docstrings, not from types.

Reviewed at commit `5cefcff` (29 Oct 2026): `bluecore_graph.py` is 1,345 lines, has 43 commits of history, and 67 tests in `test_bluecore_graph.py`.

> *Update:* the refactor's baseline is `7f0064c`, `main` after #201 merged. At that commit the file is 1,407 lines, 82 tests pass (78 in `test_bluecore_graph.py`, counting parametrised cases, and 4 in `test_save_graph_deadlocks.py`), and branch coverage is 93%.

## What the abstraction is trying to achieve

BluecoreGraph turns one arbitrary RDF payload into a consistent set of database records. Reading the code, it does four jobs:

1. **Partition** — split a graph into Hub, Work, Instance and Other Resource subgraphs, keeping anonymous descriptions inline.
2. **Reconcile identity** — reuse a Bluecore URI found via `bf:derivedFrom` (in the payload, then the database), or mint one.
3. **Apply write authority** — decide per resource whether to upsert, create-if-absent, or leave alone, depending on who is writing (bulk ingest vs. an API save naming a `primary_class`) and whether a resource is a stub.
4. **Link** — maintain `instanceOf`, `expressionOf` and the many-to-many Other Resource table in one transaction.

Jobs 1, 2 and 4 are what the class docstring's six-step heuristic describes. Job 3 is the one that has grown most, and it is the protection against re-harvest clobbering editorial work — the same concern as the snapshot vs. managed layering. The docstring barely mentions it.

## Where the code expresses it well

- **Rationale is recorded where it matters.** Comments explain why, not what: why `_find_roots` runs before `_infer`, why `_derived_from_query` renders jsonpath as a literal (to hit the functional index), why `_marked_stub` checks adminMetadata rather than searching for `mstatus/incmp`. This is unusually good institutional memory.
- **`save()` reads as the pipeline.** Mint Hubs, Works, Instances; persist each kind; flush; link; commit. The order matches the docstring.
- **Concurrency is handled as a design property.** Writes and links are sorted by URI so lock order is deterministic, and the whole save retries on deadlock, serialization failure or unique violation. The retry predicate is narrow and named.

  > *Update:* only Other Resource writes are sorted by URI, although a comment in `_persist_resources` says Works and Instances are too. Sorting them isn't needed: SQLAlchemy already issues UPDATEs in primary-key order, and a freshly minted UUID can't collide on INSERT. #213 fixes the comment, not the code.
- **Errors tell the caller what to do.** "Give the one being created an empty `@id`" is an actionable message, and `source` threads provenance into logs for bulk loads.
- **Outcome is exposed.** `created_uri` answers the API's real question — which resource did I just make — without the caller re-deriving it from the graph.
- **The read view is sensible.** `works()`, `instances()`, `hubs()`, `others()` are the right nouns for a caller inspecting a payload.

## Where the structure hides the intent

### 1. Resource role is the real concept, and it has no name

Every resource in a payload ends up in one of roughly four roles: the record being authoritatively written, a reference to another record, a stub (placeholder), or an anonymous inline description. That classification is computed piecemeal, repeatedly, by `_anonymous_description`, `_is_stub`, `_arrived_as_stub`, `_marked_stub`, `_stored_is_stub`, `_keep_existing` and `_keeps_own_links`, each consulting a different mix of `_ingest`, `_primary_class`, `_root`, `_spot_stubs`, `_described`, `_minted_from` and `_created`.

The effect is that the rule "what happens to resource X in this save" cannot be read in one place. `_persist_resources` combines three of these predicates in one condition, and `_link` combines two more. A `Role` enum assigned once per subject would make the policy legible and testable on its own.

> *Update:* a `Role` can't replace all seven. `_anonymous_description`, `_is_stub`, `_arrived_as_stub` and the reference half of `_keeps_own_links` depend only on the payload and the mode, so they become `Role`. `_keep_existing`, `_stored_is_stub` and `_created` depend on the stored row, so they become a separate `Action` (INSERT, UPDATE or KEEP), decided once the stored rows have been read.

### 2. Write mode is implied by a parameter's absence

`primary_class=None` means bulk ingest; anything else means an API save. `update_other_resources` only has meaning when `primary_class` is None. And `primary_class` accepts either a BIBFRAME `URIRef` or the `OtherResource` SQLAlchemy class — two different type systems in one argument. The comment "this is how we know a save came from a load" admits the mode is inferred, not declared. An explicit `SaveMode` (ingest, create, update) would replace `_ingest` and most of the None checks.

> *Update:* the modes are *ingest* and *write(kind)*, not ingest, create and update. bluecore-workflows' `tasks/bulk_update.py` passes `primary_class`, so a bulk update runs as a write. Whether a write creates or updates follows from the payload, depending on whether it carries a blank node for the new resource, so a caller can't declare it up front.

### 3. It is named like a value, but behaves like a one-shot command

"Graph" suggests a wrapper or view. In practice the constructor mutates the caller's graph (`_name_new_resource`, `_infer`), and `save()` sets state that changes what `works()` returns: before `save()` every root counts; after it, only `_root` does. So the accessors are temporally coupled to `save()`.

Saving the same instance twice is also not safe as far as I can tell. The retry loop inside `save()` is idempotent, but a second call recomputes `_described` from a graph that minting has already given adminMetadata, so stub detection would see every resource as described.

> *Update:* this is wrong. Saving the same `BluecoreGraph` twice works: a test with a stub in the payload gave identical rows both times. Minting writes the stub marker into the graph, and `_marked_stub` keeps a marked resource out of `_described` on the second pass. Phase 0 adds that test.

### 4. Subgraphs lose their subject, so the code keeps guessing it

`generate_entity_graph` returns a bare `Graph`, discarding which node it was built for. `_subject` then re-derives it — by type, or for Other Resources by assuming one URI subject (its own TODO calls this a guess) — dozens of times per save, with four failure modes. Carrying a small `(subject, kind, role, graph, original_uri)` record would remove the heuristic and most of those errors.

### 5. The cache invariant is maintained by hand

The subgraph cache is keyed on `_revision`, which every mutation site must remember to bump. But the cached results also depend on non-graph state (`_ingest`, `_root`), which is why `save()` bumps the revision after setting them. That works, but it is two invalidation rules disguised as one. Classifying first and freezing the graph before extraction would make the cache unnecessary or trivially correct.

### 6. adminMetadata does several jobs, by policy

It carries provenance, the identity mapping (`derivedFrom`) and the stub marker (`bf:status mstatus/incmp`). Where this is recorded is a cataloging policy decision outside this codebase, so the aim is to read it cleanly, not move it. The `derivedFrom` functional index already acts as a crosswalk that Postgres maintains from the stored JSON. A persisted computed `derived_from` column, like the existing `identifiers` column, would let `_derived_from_query` filter on a plain column instead of a jsonpath literal.

Separately, `_describe_stub_status` adds triples about the status term purely so `_extract_others` will treat it as a resource. That workaround is a symptom of Other Resources being defined by negation (any described URI object outside BF, MADS and RDF).

Minting also calls `_remove_admin_metadata` on the resource, so as far as I can tell the source record's own adminMetadata is replaced by Bluecore defaults — including `descriptionAuthentication` set to PCC for every minted resource. For catalogers, asserting PCC authentication on records that weren't PCC is a semantic claim worth checking.

## Smaller issues and doc drift

| Where | Issue |
| --- | --- |
| `_is_bibframe_resource` | Docstring says Work or Instance; code also checks Hub. |
| `_mint_all_uris` | Docstring says Works and Instances; Hubs are minted too. |
| Class docstring | Six steps omit stub handling, reference vs. primary writes, and anonymous inline nodes — most of the current logic. |
| `_generate_admin_metadata` | `try/finally` around `CURRENT_USER_ID.get()` is odd; the ContextVar has a default of None, so a plain `get() or "Unknown"` says the same thing. |
| `_link` | The Work and Instance loops are near-duplicates (its own TODO notes this), and each tests `other_uri in g.objects()` linearly: O(others × resources). |
| `_link` (Hubs) | `expressionOf` only links a Hub described in the same payload, so a Work pointing at an existing Hub by URI gets no `hub_id`. Probably intended, but undocumented at the call site. |
| `save_graph` | Returns `bg.graph`, the caller's own mutated graph, not a copy. Fine, but worth saying in the docstring. |

> *Update:* #201 merged the Work and Instance loops in `_link` and batched the lookup of held Other Resources. The Hub row stands, and #213 records it as an *inherited* row of the policy. The plan copies the input graph, so `save_graph` will return the copy and leave the caller's graph unchanged.

## Related patterns

BluecoreGraph solves three well-known problems at once, and mature systems keep them apart: boundary rules, match rules and survivorship rules.

| Concern | Established pattern | What it suggests for Bluecore |
| --- | --- | --- |
| What is a record? | DDD aggregates: one root with identity, inner value objects, other aggregates referenced by ID only. RDF: Concise Bounded Description, skolemization. | Works, Instances and Hubs are roots; inline blank nodes are value objects. "One aggregate per transaction" is the primary vs. reference rule. Deciding which blank nodes to skolemize is the job `_find_roots` does. |
| Who is this? | Entity resolution and record linkage; MDM crosswalk tables from source keys to golden-record IDs. | `derivedFrom` is exact-match linkage. Its functional index is already a crosswalk Postgres maintains. The `identifiers` column adds shared-identifier evidence for possible duplicates. |
| Whose data wins? | MDM survivorship rules and protected steward edits; ILS overlay and merge profiles; three-way merge in version control. | Stub, reference and re-ingest logic is a survivorship policy. With no base to diff against, it can only keep local edits; a stored harvest snapshot (the snapshot vs. managed layer) would supply that base. |
| How do we act safely? | Terraform `plan`/`apply`, Kubernetes reconciliation, `EXPLAIN`. | Compute a reviewable plan first, then apply it. |

A `SavePlan` could mirror these as three stages: bound the records, resolve identities, then apply survivorship.

## Suggested direction for a refactor

Split planning from applying, so the policy becomes data you can inspect and test without a database.

```python
class Role(Enum):
    PRIMARY = "primary"  # authoritatively upserted
    REFERENCE = "reference"  # create if absent, keep existing links
    STUB = "stub"  # placeholder; replaced by a real description
    INLINE = "inline"  # anonymous; stays in its parent's record


@dataclass(frozen=True)
class Entry:
    subject: IdentifiedNode
    kind: Kind  # HUB | WORK | INSTANCE | OTHER
    role: Role
    graph: Graph
    original_uri: URIRef | None


plan = SavePlan.from_graph(graph, mode=SaveMode.CREATE, primary=Kind.WORK)
plan.resolve_identities(session)  # derivedFrom lookup + minting
result = plan.apply(session)  # persist + link, with retries
```

1. **Copy, then normalise.** Take a copy of the input, run `_name_new_resource`, `_infer` and the stripping steps once, then treat the graph as frozen.
2. **Classify once.** Assign each subject a `Kind` and `Role` from the mode, roots and stub markers. This one function replaces the seven predicates, and its tests read as a policy table.
3. **Resolve identity.** Decide identity from `derivedFrom` only (optionally through a computed `derived_from` column), and collect duplicate candidates from the `identifiers` column (see Duplicate detection).
4. **Apply.** `_persist_resources` and `_link` switch on `entry.role` instead of recomputing it.

This keeps every rule the comments document; it just gives them a home. It would also align the code with the snapshot vs. managed model, where role is the thing that decides whether a harvest may overwrite.

> *Update:* #213 changes this sketch:
> - The mode is `Ingest(update_others)` or `Write(kind)`, not `SaveMode.CREATE`.
> - `Entry` has `original` rather than `original_uri`.
> - `resolve(session)` decides identities *and* actions, and only reads. It returns a resolution that `render()` can show as a dry run.
> - `apply(session_maker, max_attempts)` resolves again on a fresh session in each attempt of the retry loop, then writes.
> - The public API (`save_graph`, `BluecoreGraph(g).save()`) stays as a facade, and `SavePlan` stays internal for now.
>
> The refactor's main outcome is a statement of today's behaviour as a policy table, with each row marked *chosen* or *inherited*. The refactor itself changes no behaviour.

## Duplicate detection

> *Update:* duplicate detection is a follow-up in #213, not part of the refactor.

The reconcile step should compute possible duplicates, and the plan should carry them as advisory annotations that never change what `apply` does. Identity stays deterministic (`derivedFrom`); a shared ISBN or LCCN is only evidence, since formats, multi-volume sets and reprints legitimately share them.

```python
@dataclass(frozen=True)
class Candidate:
    uri: URIRef
    evidence: list[str]  # e.g. ["isbn:9780140449112"]
    rule: str  # "shared-identifier", later "title-creator", ...


# on Entry:
identity: Resolved | Minted  # decided by derivedFrom only
candidates: list[Candidate]  # advisory
```

- **Batched lookup.** One query per plan against the GIN-indexed `identifiers` column, using array overlap (`identifiers && ARRAY[...]`).
- **Preview before save.** The editor can run `from_graph` plus reconcile without `apply`, and warn a cataloger before anything is written.
- **Behaviour per mode.** An API create returns candidates to the cataloger; bulk ingest logs them or sends them to a review queue. That policy can be set later without touching matching.
- **More rules over time.** Works rarely share ISBNs, so work-level matching will need other evidence, probably title plus creator via `title_vector`. The `rule` field shows in the plan rendering which rule fired.

Two details to settle early: exclude the resource's own URI and anything already resolved through `derivedFrom`, or every re-save flags itself; and decide whether the review queue is a table or an event. The repo's `activity_streams_cursor` model may be relevant to the event approach, but it hasn't been reviewed here.

## Test impact

If `save_graph()` and `BluecoreGraph(g).save()` stay as a thin facade over `SavePlan.from_graph(...).apply(session)`, 57 of the 67 tests in `test_bluecore_graph.py` need no changes. They exercise the public entry points and assert on Postgres rows or the returned graph. The 4 tests in `test_save_graph_deadlocks.py` also survive, provided `apply` keeps the retry loop and the URI-sorted write order. One of them spies on `session.add` order, which makes it the most implementation-coupled.

| Bucket | Tests | After the refactor |
| --- | --- | --- |
| Public save path (`save_graph`, `.save()`) | 57 | Unchanged |
| Private methods (`_find_root` ×3, `_remove_triples_by_type`, `_derived_from_query`) | 5 | `_find_root` tests become role assertions on the plan; the other two move to the plan's normalise and identity steps |
| Accessors (`works()`, `instances()`, `hubs()`) | 4 | Become assertions on plan entries, with no database needed |
| Constructor validation (relative `@id`) | 1 | Moves to `from_graph` almost unchanged |

The tests that change are the ones checking what the plan now makes explicit, so they get simpler: asking which role a resource plays stops needing a Postgres session.

### Strengthening the net before refactoring

- [ ] Add a stable text rendering of the plan (subject, kind, role, original URI, action) for logs and debugging.
- [ ] Snapshot what the current code does to each resource for a handful of real LC records and editor payloads, and make the new plan match those snapshots row for row.
- [ ] Run branch coverage on `bluecore_graph.py` first. The weak spots are likely where the role predicates interact, such as an ingest that carries both a stub and a reference to an existing resource.

The suite has not been run for this review because it needs Postgres, so coverage figures are still open.

> *Update:* this checklist became Phase 0 of #213: a differential harness over the `uploads/` corpus, the policy statement as tests against today's code, closing the coverage gaps, and a benchmark. The suite has since been run. Branch coverage of `bluecore_graph.py` is 93% at `7f0064c`, and #213 lists the branches that are never run. The test counts above are from `5cefcff`.
