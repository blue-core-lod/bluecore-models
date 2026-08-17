from bluecore_models.utils.search import (
    SYMBOL_DELETIONS,
    SYMBOL_FOLDINGS,
    SYMBOL_SENTINELS,
    normalize_symbols,
)

# ---------------------------------------------------------------------------
# Stage 0 -- prime marks, which mean two different things depending on context
# ---------------------------------------------------------------------------


def test_prime_between_digits_becomes_a_separator():
    # Coordinate punctuation: "27 arcminutes 16 arcseconds". Deleting the marks
    # would fuse the numbers into "2716".
    assert normalize_symbols("W 118°27ʹ16ʺ") == "W 118 bcsymdegree 27 16"
    assert normalize_symbols("E 138°57ʹ10ʺ") == "E 138 bcsymdegree 57 10"


def test_prime_between_letters_is_still_deleted():
    # Same characters as Russian soft and hard signs.
    assert normalize_symbols("aktualʹnykh") == "aktualnykh"
    assert normalize_symbols("obʺekt") == "obekt"


def test_consecutive_primes_between_digits():
    # A capture-group substitution would eat the digit the next match needs and
    # produce "1 2ʹ3"; the lookarounds do not.
    assert normalize_symbols("1ʹ2ʹ3") == "1 2 3"


# ---------------------------------------------------------------------------
# Stage 1a -- deletions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Stage 1b -- foldings
# ---------------------------------------------------------------------------


def test_folds_subscript_digits():
    assert normalize_symbols("H₂O") == "H2O"
    assert normalize_symbols("C₆H₁₂O₆") == "C6H12O6"


def test_folds_superscript_digits():
    assert normalize_symbols("x² + y² = z²") == "x2 + y2 = z2"
    assert normalize_symbols("10⁻⁹") == "10-9"


def test_folds_sub_and_superscript_signs():
    assert normalize_symbols("x⁽¹⁾") == "x(1)"
    assert normalize_symbols("a₍₋₁₎") == "a(-1)"


# ---------------------------------------------------------------------------
# Stage 2 -- sentinels
# ---------------------------------------------------------------------------


def test_maps_symbols_to_space_padded_sentinels():
    # Without padding the sentinel fuses onto the preceding character.
    assert normalize_symbols("D♭ major") == "D bcsymflat  major"
    assert normalize_symbols("F♯ minor") == "F bcsymsharp  minor"


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


# ---------------------------------------------------------------------------
# Things that must not change
# ---------------------------------------------------------------------------


def test_leaves_ordinary_text_untouched():
    assert normalize_symbols("Castles & palaces--1950-1960") == (
        "Castles & palaces--1950-1960"
    )
    assert normalize_symbols("O'Brien") == "O'Brien"
    assert normalize_symbols("100% cotton") == "100% cotton"
    assert normalize_symbols("") == ""


# ---------------------------------------------------------------------------
# Table invariants
# ---------------------------------------------------------------------------


def test_deletions_are_single_characters_without_duplicates():
    assert all(len(char) == 1 for char in SYMBOL_DELETIONS)
    assert len(set(SYMBOL_DELETIONS)) == len(SYMBOL_DELETIONS)


def test_deletion_codepoints_match_their_comments():
    """The glyphs are visually identical or invisible, so the comments naming
    each codepoint are the only readable documentation -- and can drift."""
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
