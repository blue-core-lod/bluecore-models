import pytest

from bluecore_models.bluecore_graph import Ingest, Kind, Write, _kind_of, _mode
from bluecore_models.models import OtherResource
from bluecore_models.namespaces import BF


@pytest.mark.parametrize(
    "primary_class, update, expected",
    [
        (None, True, Ingest(update_others=True)),
        (None, False, Ingest(update_others=False)),
        (BF.Hub, True, Write(Kind.HUB)),
        (BF.Work, False, Write(Kind.WORK)),
        (BF.Instance, True, Write(Kind.INSTANCE)),
        (OtherResource, False, Write(Kind.OTHER)),
        (BF.Item, True, Write(None)),
        (str(BF.Work), True, Write(None)),
        (42, True, Write(None)),
    ],
)
def test_mode_from_primary_class(primary_class, update, expected):
    assert _mode(primary_class, update) == expected


def test_ingest_is_primary():
    assert all(Ingest(True).is_primary(k) for k in Kind)
    assert [k for k in Kind if not Ingest(False).is_primary(k)] == [Kind.OTHER]


def test_write_is_primary_only_for_its_kind():
    assert [k for k in Kind if Write(Kind.WORK).is_primary(k)] == [Kind.WORK]
    assert not any(Write(None).is_primary(k) for k in Kind)


def test_modes_are_values():
    # frozen dataclasses compare and hash by value; a mutable one isn't hashable
    assert {Write(Kind.WORK), Write(Kind.WORK), Ingest(True)} == {
        Write(Kind.WORK),
        Ingest(True),
    }


def test_kind_of_internal_class():
    assert [_kind_of(k.bf_class) for k in Kind] == list(Kind)
