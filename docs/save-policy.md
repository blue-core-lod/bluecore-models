# Save Policy

This is how `save_graph` treats each resource and each cited URI today.

The policy is a set of rules, one to a section. A rule names a kind of situation, and its id starts with a letter for what it covers:

- **R**: what happens to each resource
- **L**: which cited URIs a Work or Instance is linked to
- **X**: questions that sit partly outside the save

Under each rule are its cases, each headed by its id. A case is one concrete situation the rule covers: what the save does in it today, whether that has been decided, and what the code calls it. Every case is also a test, run against a real database, so this document says what the code does. A case marked *inherited* is what the code already did; nobody has chosen it yet.

A case's id is its name in `tests/policy_cases.py`, where it is defined, and its test is `test_policy_case[<id>]` in `tests/test_save_policy.py`.

This document is generated from the cases in `tests/policy_cases.py` by `uv run python tests/policy_cases.py > docs/save-policy.md`; don't edit it by hand.

## R1: ingest / blank Work / Instance / Hub

### ingest-blank-record

**A bulk load brings a Work, Instance or Hub with no URI.** A description with no identifier of its own, such as a Work described inside an Instance, is not saved as a record. It is kept as part of the record that mentions it.

Today: no record is made for it; its description stays inside its parent's data.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.INLINE`.

## R2: ingest / URI resource with its own adminMetadata (not our stub marker)

### ingest-described-new

**A bulk load brings a described resource we don't hold.** A new record from LC is added to Blue Core under a Blue Core identifier, and remembers the LC identifier it came from.

Today: it is inserted under a new Bluecore URI, with the normal status and a note of where it came from.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.INSERT`.

### ingest-described-ours

**A bulk load brings a described resource that already has a Bluecore URI.** When a description that already carries its Blue Core identifier is loaded, it replaces what we hold.

Today: the stored description is replaced by the incoming one.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.UPDATE`.

### ingest-described-held-as-stub

**A bulk load brings a full description of a resource we only hold as a stub.** A placeholder we kept because another record pointed at it is replaced as soon as a record that really describes it arrives.

Today: the stub's description is replaced and the incomplete marker is removed.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.UPDATE`.

### ingest-described-held-and-edited

**A bulk load brings a described resource we hold in full, and a cataloger has edited it.** LC sends a record again after you've edited it: your description is kept, but its subject and agent links are replaced with LC's.

Today: the stored description is kept; its Other Resource links are rebuilt from the incoming record.

Decision: *inherited*, LC overlay policy.

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.KEEP`.

## R3: ingest / URI resource without adminMetadata, when the record uses adminMetadata at all

### ingest-stub-new

**A record points at a resource it doesn't describe, and we don't hold it.** A resource that a record only mentions is saved as a placeholder and marked incomplete, so it can be told apart from a real description.

Today: a stub is inserted, marked incomplete rather than with the normal status.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.STUB`, `Action.INSERT`.

### ingest-stub-held

**A record points at a resource it doesn't describe, and we hold a full description.** A record that only mentions a resource never replaces a full description we already hold.

Today: the stored description is left exactly as it was.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.STUB`, `Action.KEEP`.

### ingest-stub-held-with-links

**A record points at a resource it doesn't describe, and we hold it with links.** A record that only mentions a resource does not remove the agents and subjects already linked to it.

Today: the resource keeps the links its own description gave it.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.STUB`, `Action.KEEP`.

### ingest-stub-new-citing-held-other

**A record's stub cites an Other Resource we already hold, by URI.** A placeholder created from a mention is still linked to the agents and subjects it cites, when we already hold them.

Today: the new stub is linked to that Other Resource.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.STUB`, `Action.INSERT`.

## R4: ingest / any URI resource, when the record has no adminMetadata

### ingest-no-admin-metadata

**A bulk-loaded record has URI Works and no adminMetadata anywhere.** When a record gives no administrative information at all, we can't tell which Works are placeholders, so all of them are treated as real descriptions.

Today: every Work is inserted as a full description, none marked incomplete.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.INSERT`.

## R5: ingest / Other Resource

### ingest-other-new

**A bulk load cites a described Other Resource we don't hold.** A new agent, subject or other authority described in a loaded record is added with the label the record gives it.

Today: the Other Resource is inserted with the payload's description.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.OTHER`, `Role.REFERENCE`, `Action.INSERT`.

### ingest-other-held-updating-others

**A bulk load describes an Other Resource we hold, and updating Other Resources is on.** By default, a loaded record's description of an agent or subject replaces the one we hold.

Today: the stored description is replaced by the new one.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.OTHER`, `Role.PRIMARY`, `Action.UPDATE`.

### ingest-other-held-not-updating-others

**A bulk load describes an Other Resource we hold, and updating Other Resources is off.** When the loader is told not to update agents and subjects, the ones we hold keep their descriptions.

Today: the stored description is unchanged.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.OTHER`, `Role.REFERENCE`, `Action.KEEP`.

## R6: write(K) / the root blank node of K

### write-root-blank

**A save through the API has no URI on the resource being created.** When you create a new resource in the editor, it is given a new identifier and saved.

Today: a new record is created for it and its URI is reported as created.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.INSERT`.

## R7: write(K) / URI resource of K

### write-uri-new

**A save through the API names a resource of the saved kind by a new Bluecore URI.** Saving a Work under a Blue Core identifier we don't hold yet creates it under that identifier.

Today: a record is created at that URI.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.INSERT`.

### write-uri-held

**A save through the API names a resource of the saved kind that we hold.** Saving a resource from the editor replaces what we hold.

Today: the stored description is replaced by the saved one.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.UPDATE`.

## R8: write(K) / any other blank node

### write-nested-blank

**A save through the API has a nested Work with no URI.** A description inside another one, with no identifier, stays part of the resource that holds it.

Today: no record is made for it; it stays inside its parent's data.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.INLINE`.

## R9: write(K) / resource of another kind

### write-reference-new

**A save through the API refers to a resource of another kind that we don't hold.** If you link to a resource we don't have yet, it is created with whatever the payload says about it.

Today: a record is created for it from what the payload says about it.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.INSTANCE`, `Role.REFERENCE`, `Action.INSERT`.

### write-reference-held

**A save through the API refers to a resource of another kind that we hold.** Linking to an existing resource from another one never changes the linked resource's description.

Today: the stored description is unchanged.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.REFERENCE`, `Action.KEEP`.

### write-reference-held-with-links

**A save through the API refers to a held resource that has Other Resource links.** Saving an Instance does not remove the agents and subjects linked to its Work.

Today: the referenced resource's links are unchanged.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.REFERENCE`, `Action.KEEP`.

## R10: write(K) / Other Resource

### write-other-held

**A save through the API cites an Other Resource that we hold.** Citing an agent or subject from a Work never shrinks the fuller description we already hold.

Today: the stored description is unchanged.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.OTHER`, `Role.REFERENCE`, `Action.KEEP`.

### write-other-as-saved-kind

**A save through the API writes an Other Resource as the thing being saved.** Saving an agent or subject on its own replaces what we hold for it.

Today: the stored description is replaced by the saved one.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.OTHER`, `Role.PRIMARY`, `Action.UPDATE`.

## R11: write(K) / (all)

### write-held-as-stub

**A save through the API writes a resource held as an incomplete stub.** Once you save a placeholder from the editor, it counts as a real description and is no longer marked incomplete.

Today: the incomplete marker is removed and the normal status is set.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.UPDATE`.

## L1: described in the payload, or already held in `other_resources`

### link-described-in-payload

**A Work cites an authority or agent and the payload describes it.** A subject or agent you pick in the editor is linked to the record.

Today: the Work is linked to that Other Resource.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.OTHER`.

### link-held-by-uri

**A Work cites an authority by URI alone, and we hold it from an earlier save.** Citing a subject or agent we already hold links it, even when the editor sends only its identifier.

Today: the Work is linked to the held Other Resource.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.OTHER`.

## L2: neither described nor held

### link-unknown-uri

**A Work cites a URI by identifier alone, and we hold nothing about it.** A subject or agent we know nothing about is kept as a plain reference, with no label and no link.

Today: no Other Resource is made and no link is saved; the URI stays in the Work's data.

Decision: *inherited*, #169 stub rows.

In the code: `Citation.OTHER`.

## L3: a BIBFRAME, MADS or RDF vocabulary term

### link-vocabulary-term

**A Work cites a BIBFRAME vocabulary term, which is also held as an Other Resource.** Terms from the BIBFRAME vocabulary, such as a content type, are never linked as Other Resources.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.VOCABULARY`.

## L4: typed as a Work, Instance or Hub in the payload

### link-record-typed-in-payload

**A Work cites a URI the payload describes as a Work, and we hold it as an Other Resource.** A resource that the record says is a Work is a record of its own, never an agent or subject link.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.RECORD`.

## L5: object of `instanceOf`, `hasInstance`, `expressionOf`, `hasExpression`, `relatedTo`, `hasItem`, `itemOf`, `bf:associatedResource` or `bflc:associatedResource`

### link-through-instanceof

**A resource cites a held, untyped URI through instanceOf, which the save takes to be a record.** Naming an existing agent or subject as the Work an Instance belongs to creates a new Work record for it, rather than linking the one we hold.

Today: the URI is replaced by a newly minted record of the inferred kind; the held Other Resource is not linked and is unchanged.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.RECORD`.

### link-through-hasinstance

**A resource cites a held, untyped URI through hasInstance, which the save takes to be a record.** Naming an existing agent or subject as an Instance of a Work creates a new Instance record for it, rather than linking the one we hold.

Today: the URI is replaced by a newly minted record of the inferred kind; the held Other Resource is not linked and is unchanged.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.RECORD`.

### link-through-expressionof

**A resource cites a held, untyped URI through expressionOf.** A resource cited as the Hub a Work expresses is never linked as an agent or subject, even if we hold it that way.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

### link-through-hasexpression

**A resource cites a held, untyped URI through hasExpression.** A Work that names a resource as one of its expressions never links it as an agent or subject, even if we hold it that way.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

### link-through-relatedto

**A resource cites a held, untyped URI through relatedTo.** A resource cited as a related Work is never linked as an agent or subject, even if we hold it that way.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

### link-through-hasitem

**A resource cites a held, untyped URI through hasItem.** A resource cited as an Item an Instance holds is never linked as an agent or subject, even if we hold it that way.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

### link-through-itemof

**A resource cites a held, untyped URI through itemOf.** An Instance that names a resource as the Instance it is an item of never links it as an agent or subject, even if we hold it that way.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

### link-through-bf-associatedresource

**A resource cites a held, untyped URI through bf-associatedResource.** A resource cited as an associated resource (BIBFRAME's property) is never linked as an agent or subject, even if we hold it that way.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

### link-through-bflc-associatedresource

**A resource cites a held, untyped URI through bflc-associatedResource.** A resource cited as an associated resource (the LC extension's property) is never linked as an agent or subject, even if we hold it that way.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

## L6: under `http://id.loc.gov/resources/`

### link-lc-resources-prefix

**A Work cites, as a subject, a held Other Resource under id.loc.gov/resources/.** LC's Works, Instances, Hubs and Items are never linked as agents or subjects, however they are cited.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.EXCLUDED`.

## L7: cited only under `bf:adminMetadata`

### link-admin-metadata-only-held

**A Work cites a held vocabulary term only inside its adminMetadata.** The status and other terms in a record's administrative information are not linked, or every record would link to them.

Today: no link is saved.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.ADMIN_ONLY`.

### link-admin-metadata-described

**A Work cites a vocabulary term inside its adminMetadata, and the payload describes the term.** An administrative term is linked only when the record describes it itself.

Today: the Work is linked to the term.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.ADMIN_ONLY`.

## L8: an LC Item

### link-lc-item

**An Instance cites an LC Item that the payload describes.** An LC Item is kept, but is not linked to its Instance.

Today: the Item is stored as an Other Resource but the Instance is not linked to it.

Decision: *inherited*, Items, decided in Phase 3.

In the code: `Citation.ITEM`.

## X1: Hub links and labels: `expressionOf` only sets `hub_id` when the payload describes the Hub

### hub-link-described

**A Work is an expressionOf a Hub that the payload describes.** A Work is attached to its Hub when the record describes that Hub.

Today: the Work is linked to the Hub.

Decision: *inherited*, review before the dev reload (#221).

In the code: `Citation.RECORD`.

### hub-link-not-described

**A Work is saved as an expressionOf a Hub we hold, but the payload does not describe or type it.** Pointing a Work at a Hub we already have fails to save unless the record also says that it is a Hub.

Today: the save fails with a duplicate-URI error and nothing is written; the Work is not linked to the Hub.

Decision: *inherited*, Hub labels and links.

In the code: `Citation.EXCLUDED`.

### hub-link-hub-not-stored

**A Work is saved as an expressionOf a Hub we don't hold, and the payload does not describe or type it.** Pointing a Work at a Hub we don't have, without describing it, saves the Hub's identifier as if it were an agent or subject, and doesn't attach the Work to it.

Today: the Work is not linked to a Hub; an Other Resource is created at the Hub's URI.

Decision: *inherited*, Hub labels and links.

In the code: `Citation.EXCLUDED`.

## X2: Re-ingest links: on re-ingest, a PRIMARY resource that `_keep_existing` protects keeps its stored description, but its Other Resource links are rebuilt from the incoming record

### reingest-of-edited-record

**A bulk load brings again an LC record a cataloger has edited, and its subject differs.** LC sends a record again after you've changed its subject: your description keeps your subject, but the subject link goes back to LC's.

Today: the edited description is kept, and its Other Resource links are rebuilt from the incoming record.

Decision: *inherited*, LC overlay policy.

In the code: `Kind.WORK`, `Role.PRIMARY`, `Action.KEEP`.

## Changing the policy

Each change is its own pull request. It changes the cases in `tests/policy_cases.py`, marks them *chosen*, changes the code to match, and regenerates this document, whose diff then shows the change in plain words. The pull request also says whether records saved under the old rule need fixing.

A case's id names its situation, not its outcome, so it stays the same when the outcome changes. A removed case's id isn't reused.
