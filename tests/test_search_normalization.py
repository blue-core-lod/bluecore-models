from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.models import ResourceBase, Work
from bluecore_models.utils.search import (
    SYMBOL_DELETIONS,
    SYMBOL_FOLDINGS,
    SYMBOL_SENTINELS,
    normalize_symbols,
)


def test_prime_between_digits_becomes_a_separator():
    # Coordinate punctuation: "27 arcminutes 16 arcseconds". Deleting the marks
    # would fuse the numbers into "2716".
    assert normalize_symbols("W 118°27ʹ16ʺ") == "W 118 bcsymdegree 27 16"
    assert normalize_symbols("E 138°57ʹ10ʺ") == "E 138 bcsymdegree 57 10"


def test_prime_between_letters_is_still_deleted():
    assert normalize_symbols("aktualʹnykh") == "aktualnykh"
    assert normalize_symbols("obʺekt") == "obekt"


def test_consecutive_primes_between_digits():
    # A capture-group substitution would eat the digit the next match needs and
    # produce "1 2ʹ3"; the lookarounds do not.
    assert normalize_symbols("1ʹ2ʹ3") == "1 2 3"


def test_deletes_romanization_marks():
    # Diacritics such as the macron are left alone: unaccent handles them in SQL.
    assert normalize_symbols("Saʻdī") == "Sadī"
    assert normalize_symbols("Shuʻaib") == "Shuaib"


def test_deletes_hamza():
    # U+02BC, near-identical on screen to the U+02BB ayn above.
    assert normalize_symbols("Tafsīr al-Qurʼān") == "Tafsīr al-Qurān"


def test_deletes_all_four_ligature_halves():
    assert normalize_symbols("transformat︠s︡ii") == "transformatsii"
    assert normalize_symbols("Mongol n︢g︣ün") == "Mongol ngün"


def test_folds_subscript_digits():
    assert normalize_symbols("H₂O") == "H2O"
    assert normalize_symbols("C₆H₁₂O₆") == "C6H12O6"


def test_folds_superscript_digits():
    assert normalize_symbols("x² + y² = z²") == "x2 + y2 = z2"
    assert normalize_symbols("10⁻⁹") == "10-9"


def test_folds_sub_and_superscript_signs():
    assert normalize_symbols("10⁻⁹") == "10-9"
    assert normalize_symbols("x⁺y") == "x+y"


def test_parenthesis_forms_fold_to_space_not_parentheses():
    """ASCII parentheses are tsquery grouping operators, so folding to them
    would turn "x⁽¹⁾" into a syntax error rather than a search."""
    assert normalize_symbols("x⁽¹⁾") == "x 1 "
    assert normalize_symbols("a₍₋₁₎") == "a -1 "
    assert "(" not in normalize_symbols("x⁽¹⁾")
    assert ")" not in normalize_symbols("x⁽¹⁾")


def test_maps_symbols_to_space_padded_sentinels():
    # Without padding the sentinel fuses onto the preceding character.
    assert normalize_symbols("D♭ major") == "D bcsymflat  major"
    assert normalize_symbols("F♯ minor") == "F bcsymsharp  minor"


def test_ascii_hash_after_a_note_letter_is_a_sharp():
    """Keyboards have "#" but not U+266F, so cataloguers type "F#"."""
    assert normalize_symbols("F#") == normalize_symbols("F♯")
    assert normalize_symbols("D# major") == normalize_symbols("D♯ major")
    assert "bcsymsharp" in normalize_symbols("C#")


def test_ascii_b_after_a_note_letter_is_a_flat():
    """Keyboards have no U+266D either, so cataloguers type "Db"."""
    assert normalize_symbols("Db major") == normalize_symbols("D♭ major")
    assert normalize_symbols("Bb") == normalize_symbols("B♭")
    assert "bcsymflat" in normalize_symbols("Eb")


def test_ascii_b_in_ordinary_words_is_left_alone():
    """Guards for the flat rule, since b is an ordinary letter. Without them it
    fired on 1043 of the 1056 records in the dev database."""
    for untouched in [
        "web",
        "Feb",
        "club",
        "cab",
        "lab",
        "job",
        "Bob",
        "05eb7c5b-9939-c893-f9cf-000000000000",
    ]:
        assert normalize_symbols(untouched) == untouched

    # Not untouched: the ayn is deleted. Just check "Abī" is not A flat.
    assert "bcsymflat" not in normalize_symbols("ʻAlī ibn Abī Ṭālib")


def test_ascii_hash_elsewhere_is_left_alone():
    """Every JSON-LD record contains XMLSchema#dateTime. Without the guard its
    "a#" is a sharp, and a third of the database gains one."""
    for untouched in [
        "http://www.w3.org/2001/XMLSchema#dateTime",
        "Issue#5",
        "Item#3",
        "#5",
        "re#f",
    ]:
        assert normalize_symbols(untouched) == untouched


def test_flat_and_sharp_are_distinguishable():
    # The bug in #203: today both chop to identical tokens.
    assert normalize_symbols("D♭") != normalize_symbols("D♯")


def test_maps_natural_and_degree():
    assert "bcsymnatural" in normalize_symbols("Study in B♮")
    assert "bcsymdegree" in normalize_symbols("W 118°")


def test_maps_copyright_and_phonogram():
    # These must map before unaccent, which would rewrite them to "(C)"/"(P)".
    assert "bcsymcopyright" in normalize_symbols("©2022")
    assert "bcsymphonogram" in normalize_symbols("℗2001")


def test_leaves_ordinary_text_untouched():
    assert normalize_symbols("Castles & palaces--1950-1960") == (
        "Castles & palaces--1950-1960"
    )
    assert normalize_symbols("O'Brien") == "O'Brien"
    assert normalize_symbols("100% cotton") == "100% cotton"
    assert normalize_symbols("") == ""


def test_deletions_are_single_characters_without_duplicates():
    assert all(len(char) == 1 for char in SYMBOL_DELETIONS)
    assert len(set(SYMBOL_DELETIONS)) == len(SYMBOL_DELETIONS)


def test_deletion_codepoints_match_their_comments():
    """The glyphs are invisible or identical on screen, so the codepoint
    comments are the only readable documentation. Comments drift."""
    assert [f"U+{ord(char):04X}" for char in SYMBOL_DELETIONS] == [
        "U+02BB",  # ayn
        "U+02BC",  # alif/hamza
        "U+02B9",  # soft sign
        "U+02BA",  # hard sign
        "U+FE20",  # ligature left half
        "U+FE21",  # ligature right half
        "U+FE22",  # double tilde left half
        "U+FE23",  # double tilde right half
    ]


def test_sentinels_survive_tokenization():
    # Underscores or punctuation would split a sentinel into two tokens.
    for sentinel in SYMBOL_SENTINELS.values():
        assert sentinel.isalnum(), f"{sentinel} would not survive tokenization"
        assert sentinel.startswith("bcsym")


def test_sentinels_are_unique():
    values = list(SYMBOL_SENTINELS.values())
    assert len(values) == len(set(values))


def test_foldings_map_to_ascii():
    for source, replacement in SYMBOL_FOLDINGS.items():
        assert len(source) == 1
        assert replacement.isascii()


def test_tables_do_not_overlap():
    deletions = set(SYMBOL_DELETIONS)
    foldings = set(SYMBOL_FOLDINGS)
    sentinels = set(SYMBOL_SENTINELS)
    assert not deletions & foldings
    assert not deletions & sentinels
    assert not foldings & sentinels


# Probes for the parity check below. The index normalizes in SQL and the query
# side in Python, so if the two disagree, searches silently stop matching.
PARITY_CORPUS = [
    # Romanization marks, taken from records in the Blue Core database.
    "Saʻdī. Gulistān",
    "Tafsīr al-Qurʼān",
    "O nekotorykh aktualʹnykh problemakh transformat︠s︡ii sistemy",
    "Mongol n︢g︣ün bichig",
    "obʺekt",
    # Music signs, and the keyboard spellings cataloguers type instead.
    "Nocturne in D♭ major",
    "Sonata in F♯ minor",
    "Study in B♮",
    "Db major",
    "F# minor",
    # Text that must not be mistaken for a sharp or a flat.
    "web Feb club cab lab job Bob",
    "ʻAlī ibn Abī Ṭālib",
    "05eb7c5b-9939-c893-f9cf-000000000000",
    "http://www.w3.org/2001/XMLSchema#dateTime",
    "Issue#5",
    # Symbols that unaccent rewrites to (C) and (P) if left to it.
    "Previous editions ©2022, 2019, and 2016.",
    "℗2001 Sony",
    # Coordinates, where the prime marks are punctuation rather than marks.
    "(W 118°27ʹ16ʺ--W 118°11ʹ43ʺ/N 34°09ʹ48ʺ--N 33°51ʹ21ʺ)",
    "1ʹ2ʹ3",
    # Sub and superscripts.
    "The chemistry of H₂O and C₆H₁₂O₆",
    "A proof that x² + y² = z²",
    "Notation for x⁽¹⁾ and a₍₋₁₎ in series",
    # Non-targets matter as much: a renderer that mangled apostrophes would pass
    # a corpus made only of symbols.
    "Castles & palaces--1950-1960",
    "O'Brien",
    "100% cotton",
    "",
]


def test_sql_and_python_normalization_agree(
    pg_session: sessionmaker[Session],
) -> None:
    """Python does stages 0 to 2, Postgres does unaccent, so
    unaccent(normalize_symbols(x)) must equal bluecore_normalize(x).

    One query for the whole corpus, so a broken pattern reports every probe it
    breaks rather than just the first."""
    with pg_session() as session:
        mismatches = (
            session.execute(
                text(
                    "select probe, bluecore_normalize(probe), unaccent(pynorm) "
                    "from unnest(cast(:probes as text[]), cast(:pynorms as text[])) "
                    "as t(probe, pynorm) "
                    "where bluecore_normalize(probe) is distinct from unaccent(pynorm)"
                ),
                {
                    "probes": PARITY_CORPUS,
                    "pynorms": [normalize_symbols(p) for p in PARITY_CORPUS],
                },
            )
            .mappings()
            .all()
        )
        assert not mismatches, f"SQL and Python disagree on: {mismatches}"


def test_sql_normalization_end_to_end(pg_session: sessionmaker[Session]) -> None:
    """Spot checks of the SQL rendering, independent of the Python one. If both
    renderings were wrong in the same way, parity alone would not notice."""
    with pg_session() as session:

        def norm(value: str) -> str:
            return session.execute(select(func.bluecore_normalize(value))).scalar_one()

        assert norm("Saʻdī") == "Sadi"
        assert norm("transformat︠s︡ii") == "transformatsii"
        assert norm("C₆H₁₂O₆") == "C6H12O6"
        assert norm("D♭ major") == "D bcsymflat  major"
        # The copyright sign must map before unaccent rewrites it to "(C)".
        assert norm("©2022") == " bcsymcopyright 2022"
        # Stage 0: coordinate primes separate, soft signs still vanish.
        assert norm("W 118°27ʹ16ʺ") == "W 118 bcsymdegree 27 16"
        assert norm("aktualʹnykh") == "aktualnykh"


def test_bluecore_normalize_is_immutable(pg_session: sessionmaker[Session]) -> None:
    """Generated columns reject non-IMMUTABLE functions, so data_vector cannot
    be built from this unless the volatility is right."""
    with pg_session() as session:
        volatility = session.execute(
            text("select provolatile from pg_proc where proname = 'bluecore_normalize'")
        ).scalar_one()
        assert volatility == "i"


def _add_work(session, uri: str, main_title: str) -> None:
    """Insert a Work so data_vector is computed by Postgres from its data.

    The data needs @id and @type: ResourceBase frames the JSON-LD on insert,
    and a fragment without them frames down to an empty object.
    """
    session.add(
        Work(
            uri=uri,
            data={
                "@id": uri,
                "@type": ["Text", "Work", "Monograph"],
                "title": {"@type": "Title", "mainTitle": main_title},
            },
        )
    )
    session.commit()


def _matches(session, query: str, uri: str) -> bool:
    stmt = select(ResourceBase).where(
        func.to_tsquery("simple", query).op("@@")(ResourceBase.data_vector),
        ResourceBase.uri == uri,
    )
    return session.execute(stmt).scalars().first() is not None


def test_index_distinguishes_flat_from_sharp(
    pg_session: sessionmaker[Session],
) -> None:
    """The bug in bluecore_api#203: the parser drops the musical signs, so a
    search for one returns records containing the other."""
    uri = "https://bluecore.info/works/symbol-flat"
    with pg_session() as session:
        _add_work(session, uri, "Nocturne in D♭ major")
        assert _matches(session, "d & bcsymflat & major", uri)
        assert not _matches(session, "d & bcsymsharp & major", uri)
        # Searching without the symbol must keep working.
        assert _matches(session, "d & major", uri)


def test_index_strips_romanization_marks(pg_session: sessionmaker[Session]) -> None:
    """unaccent turns the ayn into an apostrophe, which the parser splits on,
    so "Sadi" finds nothing until the mark is deleted first."""
    uri = "https://bluecore.info/works/symbol-romanization"
    with pg_session() as session:
        _add_work(session, uri, "Saʻdī. Gulistān")
        assert _matches(session, "sadi", uri)
        assert _matches(session, "gulistan", uri)


def test_index_folds_subscripts(pg_session: sessionmaker[Session]) -> None:
    """Subscripts act as separators, so C₆H₁₂O₆ indexes as bare c, h, o."""
    uri = "https://bluecore.info/works/symbol-subscript"
    with pg_session() as session:
        _add_work(session, uri, "The chemistry of H₂O and C₆H₁₂O₆")
        assert _matches(session, "h2o", uri)
        assert _matches(session, "c6h12o6", uri)


def test_index_deletes_ligature_halves(pg_session: sessionmaker[Session]) -> None:
    uri = "https://bluecore.info/works/symbol-ligature"
    with pg_session() as session:
        _add_work(session, uri, "O nekotorykh aktualʹnykh problemakh transformat︠s︡ii")
        assert _matches(session, "transformatsii", uri)
        assert _matches(session, "aktualnykh", uri)
