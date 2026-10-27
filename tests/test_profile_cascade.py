"""A change to a nested profile reaching every profile that nests it."""

from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.models import Profile, Version
from bluecore_models.models.profile import cascade_nested_versions, latest_version_uri
from bluecore_models.models.version import CURRENT_USERNAME

SINOPIA = "http://sinopia.io/vocabulary/"
BASE = "https://bcld.info/profiles"


def uri(name: str) -> str:
    return f"{BASE}/{name}"


def profile(name: str, nests: tuple[str, ...] = ()) -> list[dict]:
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


def edit(session: Session, saved: Profile, remark: str = "edited") -> None:
    """Change a profile's data the way a PUT does, without committing."""
    data = deepcopy(saved.data)
    data[0][f"{SINOPIA}hasRemark"] = [{"@value": remark}]
    saved.data = data
    session.flush()


def nested_refs(saved: Profile) -> list[str]:
    return sorted(
        obj["@id"]
        for node in saved.data
        for obj in node.get(f"{SINOPIA}hasResourceTemplateId", [])
    )


def version_count(session: Session, saved: Profile) -> int:
    return session.scalars(
        select(func.count()).select_from(Version).where(Version.resource_id == saved.id)
    ).one()


def test_a_parent_pins_the_childs_new_version(pg_session: sessionmaker[Session]):
    with pg_session() as session:
        title = add(session, "title")
        work = add(session, "work", nests=(uri("title"),))
        versions_before = version_count(session, work)

        edit(session, title)
        updated = cascade_nested_versions(session, title)
        session.commit()

        assert updated == [work]
        assert nested_refs(work) == [latest_version_uri(session, title)]
        assert version_count(session, work) == versions_before + 1


def test_the_cascade_goes_all_the_way_up(pg_session: sessionmaker[Session]):
    with pg_session() as session:
        title = add(session, "title")
        work = add(session, "work", nests=(uri("title"),))
        monograph = add(session, "monograph", nests=(uri("work"),))

        edit(session, title)
        updated = cascade_nested_versions(session, title)
        session.commit()

        assert updated == [work, monograph]
        assert nested_refs(monograph) == [latest_version_uri(session, work)]


def test_a_profile_reached_twice_gets_one_new_version(
    pg_session: sessionmaker[Session],
):
    """Monograph nests Title through both Work and Instance. It is saved once,
    after both, so it pins the new version of each."""
    with pg_session() as session:
        # created first, so that its id doesn't put it after what it nests
        monograph = add(session, "monograph")
        title = add(session, "title")
        work = add(session, "work", nests=(uri("title"),))
        instance = add(session, "instance", nests=(uri("title"),))
        monograph.data = profile("monograph", nests=(uri("work"), uri("instance")))
        session.commit()
        versions_before = version_count(session, monograph)

        edit(session, title)
        updated = cascade_nested_versions(session, title)
        session.commit()

        assert updated[-1] == monograph
        assert version_count(session, monograph) == versions_before + 1
        assert nested_refs(monograph) == sorted(
            [latest_version_uri(session, work), latest_version_uri(session, instance)]
        )


def test_a_parent_already_pinned_to_the_newest_is_left_alone(
    pg_session: sessionmaker[Session],
):
    with pg_session() as session:
        title = add(session, "title")
        add(session, "work", nests=(uri("title"),))
        edit(session, title)
        cascade_nested_versions(session, title)
        session.commit()

        assert cascade_nested_versions(session, title) == []


def test_new_versions_have_the_author_of_the_change(
    pg_session: sessionmaker[Session],
):
    with pg_session() as session:
        title = add(session, "title")
        work = add(session, "work", nests=(uri("title"),))

        token = CURRENT_USERNAME.set("cataloger")
        try:
            edit(session, title)
            cascade_nested_versions(session, title)
            session.commit()
        finally:
            CURRENT_USERNAME.reset(token)

        newest = session.scalars(
            select(Version)
            .where(Version.resource_id == work.id)
            .order_by(Version.id.desc())
        ).first()
        assert newest is not None
        assert newest.keycloak_username == "cataloger"


def test_a_nesting_cycle_stops(pg_session: sessionmaker[Session]):
    with pg_session() as session:
        a = add(session, "a")
        b = add(session, "b", nests=(uri("a"),))
        c = add(session, "c", nests=(uri("b"),))
        # close the cycle: a nests c
        a.data = profile("a", nests=(uri("c"),))
        session.commit()

        edit(session, a)
        updated = cascade_nested_versions(session, a)
        session.commit()

        assert updated == [b, c]


def test_only_the_references_change(pg_session: sessionmaker[Session]):
    with pg_session() as session:
        title = add(session, "title")
        work = add(session, "work", nests=(uri("title"),))
        before = deepcopy(work.data)

        edit(session, title)
        cascade_nested_versions(session, title)
        session.commit()

        before[1][f"{SINOPIA}hasResourceTemplateId"] = [
            {"@id": latest_version_uri(session, title)}
        ]
        assert work.data == before
