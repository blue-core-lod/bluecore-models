"""Profile nesting: what a profile's data says, recorded as a real relation."""

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.models import Profile, ProfileRelation

SINOPIA = "http://sinopia.io/vocabulary/"

BASE = "https://bcld.info/profiles"


def uri(name: str) -> str:
    return f"{BASE}/{name}"


def profile(name: str, nests: tuple[str, ...] = ()) -> list[dict]:
    """Expanded JSON-LD for a Sinopia profile, optionally nesting others.

    Matches how Sinopia serializes these, and the two predicates differ: a
    profile's own id is a literal, while a reference to a nested profile is an
    IRI reference. That asymmetry is why extraction reads the graph rather than
    assuming one shape.
    """
    doc: list[dict] = [
        {
            "@id": uri(name),
            "@type": [f"{SINOPIA}ResourceTemplate"],
            f"{SINOPIA}hasResourceId": [{"@value": name, "@language": "en"}],
        }
    ]
    for i, nested in enumerate(nests):
        doc.append(
            {
                "@id": f"_:attributes{i}",
                "@type": [f"{SINOPIA}ResourcePropertyTemplate"],
                f"{SINOPIA}hasResourceTemplateId": [{"@id": nested}],
            }
        )
    return doc


def add(session: Session, name: str, nests: tuple[str, ...] = ()) -> Profile:
    saved = Profile(uri=uri(name), data=profile(name, nests))
    session.add(saved)
    session.commit()
    return saved


def delete_profile(session: Session, saved: Profile) -> None:
    """Delete a profile the way bluecore_api's DELETE /profiles/{uuid} does.

    ResourceBase.versions and .classes have no delete cascade, and both foreign
    keys are NOT NULL, so SQLAlchemy's default de-association would try to null
    them and fail. Every caller has to clear them by hand -- see
    bluecore_api/app/routes/profiles.py:163-167. profile_relations is the one
    side that does cascade, which is the point of these tests.
    """
    for bf_class in saved.classes:
        session.delete(bf_class)
    for version in saved.versions:
        session.delete(version)
    session.delete(saved)
    session.commit()


def edges(session: Session, parent: Profile) -> set[int]:
    return set(
        session.scalars(
            select(ProfileRelation.child_profile_id).where(
                ProfileRelation.parent_profile_id == parent.id
            )
        )
    )


def test_saving_a_parent_records_one_edge_per_reference(
    pg_session: sessionmaker[Session],
):
    with pg_session() as session:
        child = add(session, "Note:General")
        other = add(session, "Note:Language")
        parent = add(
            session, "Work:Map", nests=(uri("Note:General"), uri("Note:Language"))
        )

        assert edges(session, parent) == {child.id, other.id}


def test_parents_and_children_resolve_both_directions(
    pg_session: sessionmaker[Session],
):
    with pg_session() as session:
        child = add(session, "Title:Parallel")
        parent = add(session, "Instance:Monograph", nests=(uri("Title:Parallel"),))

        assert [p.id for p in child.parents] == [parent.id]
        assert [c.id for c in parent.children] == [child.id]
        assert parent.parents == []
        assert child.children == []


def test_save_order_does_not_matter(pg_session: sessionmaker[Session]):
    """A parent saved before its child picks the edge up when the child lands.

    Nothing re-reads the parent, so this holds only because the parent is saved
    again afterwards -- which is what a bulk ingest re-sync would do.
    """
    with pg_session() as session:
        parent = add(session, "Work:Text", nests=(uri("Agent:Only"),))
        assert edges(session, parent) == set()

        child = add(session, "Agent:Only")
        parent.data = profile("Work:Text", ("https://bcld.info/profiles/Agent:Only",))
        session.commit()

        assert edges(session, parent) == {child.id}


def test_a_profile_can_be_nested_by_several_parents(pg_session: sessionmaker[Session]):
    """The case a single parent_id column could not express.

    On stage 33 of 91 nested profiles have more than one parent, one of them 14.
    """
    with pg_session() as session:
        child = add(session, "Note:Shared")
        first = add(session, "Work:One", nests=(uri("Note:Shared"),))
        second = add(session, "Work:Two", nests=(uri("Note:Shared"),))
        third = add(session, "Work:Three", nests=(uri("Note:Shared"),))

        assert {p.id for p in child.parents} == {first.id, second.id, third.id}


def test_a_profile_stays_nested_until_the_last_parent_goes(
    pg_session: sessionmaker[Session],
):
    """Deleting one of several parents must not make the child look top-level.

    This is bluecore-models#160, which the earlier is_nested flag got wrong: the
    flag stayed set forever once its only parent was deleted. Here the cascade
    removes the evidence and the answer re-derives itself.
    """
    with pg_session() as session:
        child = add(session, "Status:Only")
        first = add(session, "Parent:One", nests=(uri("Status:Only"),))
        second = add(session, "Parent:Two", nests=(uri("Status:Only"),))

        delete_profile(session, first)
        reloaded = session.get(Profile, child.id)
        assert reloaded is not None
        assert [p.id for p in reloaded.parents] == [second.id]

        delete_profile(session, second)
        reloaded = session.get(Profile, child.id)
        assert reloaded is not None
        assert reloaded.parents == []


def test_deleting_a_child_removes_its_edges(pg_session: sessionmaker[Session]):
    """ON DELETE CASCADE, not a listener.

    The string-keyed design left rows naming a deleted child in place.
    """
    with pg_session() as session:
        child = add(session, "Doomed:Child")
        parent = add(session, "Surviving:Parent", nests=(uri("Doomed:Child"),))
        assert edges(session, parent) == {child.id}

        delete_profile(session, child)

        assert edges(session, parent) == set()


def test_editing_a_parent_adds_and_drops_edges(pg_session: sessionmaker[Session]):
    with pg_session() as session:
        kept = add(session, "Kept:Child")
        dropped = add(session, "Dropped:Child")
        added = add(session, "Added:Child")
        parent = add(
            session, "Mutable:Parent", nests=(uri("Kept:Child"), uri("Dropped:Child"))
        )
        assert edges(session, parent) == {kept.id, dropped.id}

        parent.data = profile("Mutable:Parent", (uri("Kept:Child"), uri("Added:Child")))
        session.commit()

        assert edges(session, parent) == {kept.id, added.id}


def test_a_reference_naming_nothing_stored_records_no_edge(
    pg_session: sessionmaker[Session],
):
    """One of 161 edges on stage resolves to nothing. It must not raise."""
    with pg_session() as session:
        parent = add(session, "Haunted:Parent", nests=(uri("Ghost"),))

        assert edges(session, parent) == set()
        assert parent.children == []


def test_a_reference_to_a_work_records_no_edge(pg_session: sessionmaker[Session]):
    """Only a profile can be either end, so a non-profile URI resolves to nothing."""
    with pg_session() as session:
        work_uri = "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb"
        parent = add(session, "Confused:Parent", nests=(work_uri,))

        assert edges(session, parent) == set()


def test_a_profile_naming_itself_is_not_nested(pg_session: sessionmaker[Session]):
    """The CHECK constraint forbids the row, so it is never offered one."""
    with pg_session() as session:
        looping = add(session, "Self:Loop", nests=(uri("Self:Loop"),))

        assert edges(session, looping) == set()
        assert looping.parents == []
        assert looping.is_nested is False


def test_is_nested_filters_in_sql(pg_session: sessionmaker[Session]):
    """select(...).where(~Profile.is_nested) is how the API excludes nested profiles."""
    with pg_session() as session:
        child = add(session, "Filter:Child")
        parent = add(session, "Filter:Parent", nests=(uri("Filter:Child"),))

        nested: set[int] = set(
            session.scalars(select(Profile.id).where(Profile.is_nested))
        )
        top_level: set[int] = set(
            session.scalars(select(Profile.id).where(~Profile.is_nested))
        )

        assert child.id in nested
        assert parent.id in top_level
        assert child.id not in top_level

        # The filter and the relationship must agree on every row.
        found: Profile
        for found in session.scalars(select(Profile)):
            assert found.is_nested == bool(found.parents)


def test_is_nested_is_not_carried_by_an_ordinary_query(
    pg_session: sessionmaker[Session],
):
    """deferred, so unrelated profile queries do not pay for the subquery."""
    assert "EXISTS" not in str(select(Profile))
    assert "EXISTS" in str(select(Profile).where(~Profile.is_nested))


def test_saving_does_not_rewrite_the_stored_document(
    pg_session: sessionmaker[Session],
):
    """Profiles come back out in the shape they went in.

    profile_refs() parses a copy because load_jsonld writes an @context into any
    dict it is handed.
    """
    with pg_session() as session:
        saved = Profile(uri=uri("Untouched"), data={"label": "unchanged"})
        session.add(saved)
        session.commit()

        assert saved.data == {"label": "unchanged"}


def test_a_compacted_document_is_read_the_same(pg_session: sessionmaker[Session]):
    """A reference is found whether the document is expanded or compacted."""
    with pg_session() as session:
        child = add(session, "Compact:Child")
        parent = Profile(
            uri=uri("Compact:Parent"),
            data={
                "@context": {"sinopia": SINOPIA},
                "@id": uri("Compact:Parent"),
                "sinopia:hasResourceTemplateId": {"@id": uri("Compact:Child")},
            },
        )
        session.add(parent)
        session.commit()

        assert edges(session, parent) == {child.id}


def test_a_save_that_does_not_touch_data_leaves_the_edges_alone(
    pg_session: sessionmaker[Session],
):
    """The has_changes guard: relinking a resource must not re-derive nesting."""
    with pg_session() as session:
        child = add(session, "Stable:Child")
        parent = add(session, "Stable:Parent", nests=(uri("Stable:Child"),))
        before: set[int] = set(
            session.scalars(
                select(ProfileRelation.id).where(
                    ProfileRelation.parent_profile_id == parent.id
                )
            )
        )
        assert len(before) == 1

        parent.uri = uri("Stable:Parent:Renamed")
        session.commit()

        after: set[int] = set(
            session.scalars(
                select(ProfileRelation.id).where(
                    ProfileRelation.parent_profile_id == parent.id
                )
            )
        )
        # Same row ids, so the rows were never deleted and reinserted.
        assert after == before
        assert edges(session, parent) == {child.id}


def test_a_legacy_id_string_reference_records_no_edge(
    pg_session: sessionmaker[Session],
):
    """The pre-migration form resolves to nothing, because refs are URIs now.

    The 20261001 migration converted every stored reference, but a client can
    still send an id string. It records no edge rather than silently matching
    the wrong profile; bluecore_api rejects it with a 422 on the way in.
    """
    with pg_session() as session:
        add(session, "Role")
        parent = add(session, "Legacy:Parent", nests=("pcc:bf2:Role",))

        assert edges(session, parent) == set()
        assert parent.children == []


def test_a_reference_written_as_a_literal_records_no_edge(
    pg_session: sessionmaker[Session],
):
    """hasResourceTemplateId should be an IRI reference, not a literal.

    A literal still reads back as its lexical form, so it resolves exactly as
    far as that string does -- which for an id string is nowhere.
    """
    with pg_session() as session:
        child = add(session, "Literal:Child")
        parent = Profile(
            uri=uri("Literal:Parent"),
            data=[
                {
                    "@id": uri("Literal:Parent"),
                    f"{SINOPIA}hasResourceTemplateId": [{"@value": "Literal:Child"}],
                }
            ],
        )
        session.add(parent)
        session.commit()

        assert edges(session, parent) == set()
        assert child.parents == []


def test_a_profile_whose_context_would_need_fetching_still_saves(
    pg_session: sessionmaker[Session],
):
    """A remote @context must never be dereferenced during a flush.

    Resolving one would mean an HTTP request inside the transaction, so the
    references go unread. The save has to succeed anyway: on main a Profile
    accepted any JSON, and tightening that here would reject documents that
    used to persist.
    """
    with pg_session() as session:
        data = {
            "@context": "https://example.invalid/context.jsonld",
            "@id": uri("Remote:Parent"),
            "sinopia:hasResourceTemplateId": {"@id": uri("Remote:Child")},
        }
        parent = Profile(uri=uri("Remote:Parent"), data=data)
        session.add(parent)
        session.commit()

        assert parent.id is not None
        assert parent.data == data
        assert edges(session, parent) == set()


def test_a_profile_whose_data_is_not_json_ld_still_saves(
    pg_session: sessionmaker[Session],
):
    """Unreadable data costs the nesting, not the save."""
    with pg_session() as session:
        for name, data in (
            ("Bad:Context", {"@context": 5, "@id": "x"}),
            ("Bad:Nodes", [1, 2, 3]),
            (
                "Bad:Language",
                [
                    {
                        "@id": "x",
                        f"{SINOPIA}hasResourceId": [{"@value": "v", "@language": 9}],
                    }
                ],
            ),
        ):
            saved = Profile(uri=uri(name), data=data)
            session.add(saved)
            session.commit()

            assert saved.id is not None
            assert saved.data == data
            assert edges(session, saved) == set()


def test_moving_a_child_from_one_parent_to_another(pg_session: sessionmaker[Session]):
    """Re-parenting is two edits, to the parents, and each touches only its own rows.

    A profile's data says nothing about what nests it, so moving a child from
    one parent to another means editing both parents, not the child. The new
    parent's save must not disturb the old parent's row, and the old parent's
    save must remove only its own.
    """
    with pg_session() as session:
        child = add(session, "Moving:Child")
        old_parent = add(session, "Old:Parent", nests=(uri("Moving:Child"),))
        new_parent = add(session, "New:Parent")
        assert [p.id for p in child.parents] == [old_parent.id]

        new_parent.data = profile("New:Parent", (uri("Moving:Child"),))
        session.commit()
        assert {p.id for p in child.parents} == {old_parent.id, new_parent.id}

        old_parent.data = profile("Old:Parent")
        session.commit()
        assert [p.id for p in child.parents] == [new_parent.id]
        assert edges(session, old_parent) == set()
        assert edges(session, new_parent) == {child.id}


def test_updating_a_child_does_not_change_who_nests_it(
    pg_session: sessionmaker[Session],
):
    """The child has no say in it.

    Saving a profile reconciles the rows where it is the parent. Rows where it
    is the child belong to whoever asserted them, so editing the child leaves
    them alone -- otherwise a cataloger editing a nested profile would silently
    orphan it from its parents.
    """
    with pg_session() as session:
        child = add(session, "Edited:Child")
        parent = add(session, "Unaware:Parent", nests=(uri("Edited:Child"),))
        assert [p.id for p in child.parents] == [parent.id]

        child.data = profile("Edited:Child", nests=(uri("Some:Other"),))
        session.commit()

        assert [p.id for p in child.parents] == [parent.id]
        assert edges(session, parent) == {child.id}
