"""Profiles carry an is_nested flag derived from the templates that reference them.

A Sinopia profile is stored as expanded JSON-LD holding one
``sinopia:ResourceTemplate``. A template becomes "nested" when some *other*
template points at it: a property template typed
``sinopia:propertyType/resource`` hangs a ``sinopia:ResourcePropertyTemplate``
off ``sinopia:hasResourceAttributes``, and that node names its targets with
``sinopia:hasResourceTemplateId``. Those ids match the target template's own
``sinopia:hasResourceId``.
"""

from bluecore_models.models import Profile

SINOPIA = "http://sinopia.io/vocabulary/"


def template(resource_id: str, nests: tuple[str, ...] = ()) -> list[dict]:
    """Build expanded JSON-LD for a template that nests the given template ids.

    Shaped like the profiles Sinopia actually stores: ``hasResourceId`` is a
    language-tagged literal, while ``hasResourceTemplateId`` is an IRI
    reference. The two forms differ, so a matcher that assumes either one alone
    silently reads no nestings.
    """
    doc: list[dict] = [
        {
            "@id": f"https://bcld.info/profiles/{resource_id}",
            "@type": [f"{SINOPIA}ResourceTemplate"],
            f"{SINOPIA}hasResourceId": [{"@value": resource_id, "@language": "en"}],
        }
    ]
    for index, nested_id in enumerate(nests):
        attributes = f"_:attributes{index}"
        doc.append(
            {
                "@id": f"_:property{index}",
                "@type": [f"{SINOPIA}PropertyTemplate"],
                f"{SINOPIA}hasPropertyType": [
                    {"@id": f"{SINOPIA}propertyType/resource"}
                ],
                f"{SINOPIA}hasResourceAttributes": [{"@id": attributes}],
            }
        )
        doc.append(
            {
                "@id": attributes,
                "@type": [f"{SINOPIA}ResourcePropertyTemplate"],
                f"{SINOPIA}hasResourceTemplateId": [{"@id": nested_id}],
            }
        )
    return doc


def by_resource_id(session, resource_id: str) -> Profile:
    return (
        session.query(Profile)
        .filter(
            Profile.data.contains(
                [{f"{SINOPIA}hasResourceId": [{"@value": resource_id}]}]
            )
        )
        .one()
    )


def test_template_nobody_references_is_not_nested(pg_session):
    with pg_session() as session:
        session.add(Profile(data=template("bluecore:bf2:Work:Monograph")))
        session.commit()

        profile = session.query(Profile).order_by(Profile.id).all()[-1]
        assert profile.is_nested is False


def test_saving_a_parent_nests_the_template_it_references(pg_session):
    with pg_session() as session:
        session.add(Profile(data=template("bluecore:bf2:Title:WorkTitle")))
        session.commit()

        session.add(
            Profile(
                data=template(
                    "bluecore:bf2:Work:Monograph",
                    nests=("bluecore:bf2:Title:WorkTitle",),
                )
            )
        )
        session.commit()

        assert by_resource_id(session, "bluecore:bf2:Title:WorkTitle").is_nested is True
        assert by_resource_id(session, "bluecore:bf2:Work:Monograph").is_nested is False


def test_template_saved_after_the_parent_that_references_it_is_nested(pg_session):
    with pg_session() as session:
        session.add(
            Profile(
                data=template(
                    "bluecore:bf2:Work:Text", nests=("bluecore:bf2:Note:Summary",)
                )
            )
        )
        session.commit()

        session.add(Profile(data=template("bluecore:bf2:Note:Summary")))
        session.commit()

        assert by_resource_id(session, "bluecore:bf2:Note:Summary").is_nested is True


def test_dropping_the_last_reference_un_nests_the_template(pg_session):
    with pg_session() as session:
        session.add(Profile(data=template("bluecore:bf2:Item:Barcode")))
        parent = Profile(
            data=template(
                "bluecore:bf2:Item:Holding", nests=("bluecore:bf2:Item:Barcode",)
            )
        )
        session.add(parent)
        session.commit()
        assert by_resource_id(session, "bluecore:bf2:Item:Barcode").is_nested is True

        parent.data = template("bluecore:bf2:Item:Holding")
        session.commit()

        assert by_resource_id(session, "bluecore:bf2:Item:Barcode").is_nested is False


def test_template_still_nested_elsewhere_stays_nested(pg_session):
    with pg_session() as session:
        session.add(Profile(data=template("bluecore:bf2:Agent:Contributor")))
        first = Profile(
            data=template(
                "bluecore:bf2:Work:Serial", nests=("bluecore:bf2:Agent:Contributor",)
            )
        )
        session.add(first)
        session.add(
            Profile(
                data=template(
                    "bluecore:bf2:Work:Score", nests=("bluecore:bf2:Agent:Contributor",)
                )
            )
        )
        session.commit()

        first.data = template("bluecore:bf2:Work:Serial")
        session.commit()

        assert (
            by_resource_id(session, "bluecore:bf2:Agent:Contributor").is_nested is True
        )


def test_one_template_can_nest_several(pg_session):
    children = (
        "bluecore:bf2:Note:General",
        "bluecore:bf2:Note:Bibliography",
        "bluecore:bf2:Note:Language",
    )
    with pg_session() as session:
        for child in children:
            session.add(Profile(data=template(child)))
        session.commit()

        session.add(Profile(data=template("bluecore:bf2:Work:Notes", nests=children)))
        session.commit()

        for child in children:
            assert by_resource_id(session, child).is_nested is True


def test_the_last_reference_dropped_un_nests_but_not_before(pg_session):
    child = "bluecore:bf2:Title:Variant"
    with pg_session() as session:
        session.add(Profile(data=template(child)))
        parents = [
            Profile(data=template(name, nests=(child,)))
            for name in (
                "bluecore:bf2:Work:Atlas",
                "bluecore:bf2:Work:Manuscript",
                "bluecore:bf2:Work:Periodical",
            )
        ]
        for parent in parents:
            session.add(parent)
        session.commit()
        assert by_resource_id(session, child).is_nested is True

        parents[0].data = template("bluecore:bf2:Work:Atlas")
        session.commit()
        assert by_resource_id(session, child).is_nested is True

        parents[1].data = template("bluecore:bf2:Work:Manuscript")
        session.commit()
        assert by_resource_id(session, child).is_nested is True

        # Only when the final parent lets go does it return to the search results.
        parents[2].data = template("bluecore:bf2:Work:Periodical")
        session.commit()
        assert by_resource_id(session, child).is_nested is False


def test_editing_a_parent_to_add_a_nesting_nests_the_existing_template(pg_session):
    """A standalone template becomes nested when an existing parent is edited
    to reference it -- the reference can arrive long after both were created."""
    with pg_session() as session:
        session.add(Profile(data=template("bluecore:bf2:Note:Performer")))
        parent = Profile(data=template("bluecore:bf2:Work:Audio"))
        session.add(parent)
        session.commit()
        assert by_resource_id(session, "bluecore:bf2:Note:Performer").is_nested is False

        parent.data = template(
            "bluecore:bf2:Work:Audio", nests=("bluecore:bf2:Note:Performer",)
        )
        session.commit()

        assert by_resource_id(session, "bluecore:bf2:Note:Performer").is_nested is True
