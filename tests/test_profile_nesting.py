"""Profiles record their nestings as rows, not as a flag.

A Sinopia profile is JSON-LD holding one ``sinopia:ResourceTemplate``. It names
itself with ``sinopia:hasResourceId`` and names the templates it nests with
``sinopia:hasResourceTemplateId``. Both are extracted on write: the id onto
``Profile.template_id``, the references into ``profile_nestings``.
"""

from bluecore_models.models import Profile, ProfileNesting

SINOPIA = "http://sinopia.io/vocabulary/"


def template(resource_id: str, nests: tuple[str, ...] = ()) -> list[dict]:
    """Expanded JSON-LD for a template, shaped the way Sinopia stores it.

    hasResourceId is a language-tagged literal; hasResourceTemplateId is an IRI
    reference. The two forms differ, so a reader that assumes one finds nothing.
    """
    doc: list[dict] = [
        {
            "@id": f"https://bcld.info/profiles/{resource_id}",
            "@type": [f"{SINOPIA}ResourceTemplate"],
            f"{SINOPIA}hasResourceId": [{"@value": resource_id, "@language": "en"}],
        }
    ]
    for index, nested_id in enumerate(nests):
        doc.append(
            {
                "@id": f"_:attributes{index}",
                "@type": [f"{SINOPIA}ResourcePropertyTemplate"],
                f"{SINOPIA}hasResourceTemplateId": [{"@id": nested_id}],
            }
        )
    return doc


def add(session, resource_id: str, nests: tuple[str, ...] = ()) -> Profile:
    profile = Profile(
        uri=f"https://bcld.info/profiles/{resource_id}",
        data=template(resource_id, nests),
    )
    session.add(profile)
    session.commit()
    return profile


def test_a_template_records_its_own_id(pg_session):
    with pg_session() as session:
        profile = add(session, "bluecore:bf2:Work:Monograph")

        assert profile.template_id == "bluecore:bf2:Work:Monograph"


def test_saving_a_parent_records_one_nesting_per_reference(pg_session):
    with pg_session() as session:
        parent = add(
            session,
            "bluecore:bf2:Work:Notes",
            nests=("bluecore:bf2:Note:General", "bluecore:bf2:Note:Language"),
        )

        recorded = {
            ref.child_template_id
            for ref in session.query(ProfileNesting).filter_by(
                parent_profile_id=parent.id
            )
        }
        assert recorded == {"bluecore:bf2:Note:General", "bluecore:bf2:Note:Language"}


def test_parents_and_children_resolve_both_directions(pg_session):
    with pg_session() as session:
        child = add(session, "bluecore:bf2:Title:WorkTitle")
        parent = add(
            session,
            "bluecore:bf2:Work:Monograph",
            nests=("bluecore:bf2:Title:WorkTitle",),
        )

        assert [p.template_id for p in parent.children()] == [
            "bluecore:bf2:Title:WorkTitle"
        ]
        assert [p.template_id for p in child.parents()] == [
            "bluecore:bf2:Work:Monograph"
        ]
        assert child.parents() != []
        assert parent.parents() == []


def test_save_order_does_not_matter(pg_session):
    """The parent may be stored before the template it names exists."""
    with pg_session() as session:
        parent = add(
            session, "bluecore:bf2:Work:Text", nests=("bluecore:bf2:Note:Summary",)
        )
        assert parent.children() == []

        child = add(session, "bluecore:bf2:Note:Summary")

        assert child.parents() != []
        assert [p.template_id for p in parent.children()] == [
            "bluecore:bf2:Note:Summary"
        ]


def test_a_template_can_be_nested_by_several_parents(pg_session):
    with pg_session() as session:
        child = add(session, "bluecore:bf2:Agent:Contributor")
        for name in ("bluecore:bf2:Work:Serial", "bluecore:bf2:Work:Score"):
            add(session, name, nests=("bluecore:bf2:Agent:Contributor",))

        assert len(child.parents()) == 2


def test_editing_a_parent_adds_and_drops_nestings(pg_session):
    with pg_session() as session:
        kept = add(session, "bluecore:bf2:Note:Kept")
        dropped = add(session, "bluecore:bf2:Note:Dropped")
        added = add(session, "bluecore:bf2:Note:Added")
        parent = add(
            session,
            "bluecore:bf2:Work:Audio",
            nests=("bluecore:bf2:Note:Kept", "bluecore:bf2:Note:Dropped"),
        )
        assert dropped.parents() != []

        parent.data = template(
            "bluecore:bf2:Work:Audio",
            nests=("bluecore:bf2:Note:Kept", "bluecore:bf2:Note:Added"),
        )
        session.commit()

        assert kept.parents() != []
        assert dropped.parents() == []
        assert added.parents() != []


def test_a_template_still_nested_elsewhere_stays_nested(pg_session):
    with pg_session() as session:
        child = add(session, "bluecore:bf2:Title:Variant")
        first = add(
            session, "bluecore:bf2:Work:Atlas", nests=("bluecore:bf2:Title:Variant",)
        )
        add(
            session,
            "bluecore:bf2:Work:Manuscript",
            nests=("bluecore:bf2:Title:Variant",),
        )

        first.data = template("bluecore:bf2:Work:Atlas")
        session.commit()

        assert child.parents() != []


def test_a_reference_to_a_template_that_is_not_stored_is_kept(pg_session):
    """Real profiles name templates that were never loaded. The document said
    it, so the row records it, and it simply resolves to nothing."""
    with pg_session() as session:
        parent = add(session, "bluecore:bf2:Work:Map", nests=("bluecore:bf2:Ghost",))

        assert [r.child_template_id for r in parent.nested_refs] == [
            "bluecore:bf2:Ghost"
        ]
        assert parent.children() == []


def test_a_template_naming_itself_is_not_nested(pg_session):
    with pg_session() as session:
        profile = add(session, "bluecore:bf2:Self", nests=("bluecore:bf2:Self",))

        assert profile.parents() == []


def test_a_compacted_document_is_read_the_same(pg_session):
    """A client may PUT a profile compacted with a @context rather than
    expanded. The nestings it asserts are the same either way."""
    with pg_session() as session:
        child = add(session, "bluecore:bf2:Title:WorkTitle")
        parent = Profile(
            uri="https://bcld.info/profiles/compacted",
            data={
                "@context": {"sinopia": SINOPIA},
                "@id": "https://bcld.info/profiles/compacted",
                "@type": "sinopia:ResourceTemplate",
                "sinopia:hasResourceId": "bluecore:bf2:Work:Compact",
                "sinopia:hasResourceTemplateId": {
                    "@id": "bluecore:bf2:Title:WorkTitle"
                },
            },
        )
        session.add(parent)
        session.commit()

        assert parent.template_id == "bluecore:bf2:Work:Compact"
        assert [p.template_id for p in child.parents()] == ["bluecore:bf2:Work:Compact"]


def test_saving_does_not_rewrite_the_stored_document(pg_session):
    """Reading the nestings must leave the document alone. load_jsonld writes a
    @context into any dict it is handed, which would otherwise be persisted."""
    with pg_session() as session:
        data = {"label": "unchanged"}
        profile = Profile(uri="https://bcld.info/profiles/plain", data=data)
        session.add(profile)
        session.commit()

        assert profile.data == {"label": "unchanged"}
