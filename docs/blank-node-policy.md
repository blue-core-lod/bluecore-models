# Blank nodes and records

When bluecore-models saves a graph, each Work, Instance and Hub in it either gets a record of its own (a row and a Blue Core URI) or stays inline in the JSON of the record that contains it. This page explains how that is decided for blank nodes.

## The rule

**No identity, no record.** A resource gets a record if it has a URI, or if it is the resource being created. Every other blank node stays inline in the record that contains it, until something gives it a URI.

- **Bulk load** (`save_graph` with no `primary_class`): every blank node stays inline.
- **API create** (POST, with a `primary_class`): the blank node being created is given a URI. Every other blank node stays inline.
- **API update** (PUT): the resource already has a URI, so every blank node stays inline.

This applies to nested Works (for example under `bf:accompaniedBy`), to Instances under `bf:hasInstance`, and to Hubs under `bf:expressionOf`.

## Why

A blank node has nothing to recognise it by the next time it arrives. If it got a URI on every save, then every reload, and every PUT that sends it again, would create another copy. Keeping it inline means the same payload always produces the same records.

## What this means for clients

- **Create a Work and its Instance separately.** POST the Work, then POST the Instance with `bf:instanceOf` pointing at the Work's URI. That is how `Instance.work_id` gets set. A blank-node Instance nested in a Work is stored inline in the Work, and doesn't appear under `/instances/` or in search.
- **Link a Work to a Hub by URI.** A blank-node Hub under `bf:expressionOf` stays inline, and `Work.hub` stays empty until the Work points at a Hub with a URI.
- **Nested descriptions round-trip.** A Work like this one is saved as a single record, with the Latin Work inline under `accompaniedBy`:

```turtle
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .

[] a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "Hildegard von Bingen's Physica"@en ] ;
    bf:accompaniedBy [
        a bf:Work ;
        bf:language <http://id.loc.gov/vocabulary/languages/lat>
    ] .
```

## Which blank node is being created

On a create, the resource being created is the blank node of the `primary_class` that no other Work, Instance or Hub points to. In the example above, that's the outer Work.

The shape alone can't settle this when a nested resource points back at its parent:

```turtle
_:a a bf:Work ;
    bf:hasPart [ a bf:Work ; bf:partOf _:a ] .
```

Each Work here is inside the other, so the save is refused with a `BluecoreGraphError` rather than a guess:

> Unable to tell which resource is being created: found 2 blank node Works, each described inside another. Give the one being created an empty @id.

A payload can name the resource being created with an empty IRI, `"@id": ""` in JSON-LD or `<>` in Turtle. This follows [Linked Data Platform 1.0](https://www.w3.org/TR/ldp/#ldpc-post-rdfnullrel), where the null relative URI in a POST names the resource being created:

```turtle
<> a bf:Work ;
    bf:hasPart [ a bf:Work ; bf:partOf <> ] .
```

For this to work, the code doing the create parses the payload with `load_jsonld(data, base=NEW_RESOURCE)`. The part then stays inline, and its `bf:partOf` points at the new Work's URI. Any other relative `@id`, such as `"#part"`, is refused, because it would resolve to a URI that looks like a real identity.

## Background

This rule came out of [bluecore-models#203](https://github.com/blue-core-lod/bluecore-models/issues/203). A new Work with a blank-node Work nested under `bf:accompaniedBy` couldn't be created, and updating it split the nested Work out into a record of its own. The bulk loader already kept blank nodes inline, after [#148](https://github.com/blue-core-lod/bluecore-models/issues/148) found them being minted again on every reload.

A possible future extension is to give a blank node a record once it meets a minimal description: a set of assertions, such as a title and a creator, that identify it well enough to match on a later save.
