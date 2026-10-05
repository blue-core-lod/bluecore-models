"""
We need to remove diacritics from the index.
Unfortunately, unaccent() is only STABLE not IMMUTABLE, which means it cannot be used in a generated column.
The workaround is to create an immutable wrapper function around unaccent() - immutable_unaccent, f_unaccent.
-- Source - https://stackoverflow.com/a/11007216
-- Posted by Erwin Brandstetter, modified by community. See post 'Timeline' for change history
-- Retrieved 2026-04-16, License - CC BY-SA 4.0

Add function jsonb_to_tsv to extract text from jsonb and convert to tsvector.
If the jsonb value is an array, it will concatenate the values of the specified key from all objects in the array.
Otherwise, it will extract the value of the specified key from the jsonb object.

Add function bluecore_normalize, which handles bibliographic symbols and ALA-LC
romanization marks that unaccent alone gets wrong.
"""

from sqlalchemy import func, literal_column
from sqlalchemy.dialects import postgresql

from bluecore_models.utils.search import (
    FLAT_AFTER_NOTE_PATTERN,
    PRIME_BETWEEN_DIGITS_PATTERN,
    SHARP_AFTER_NOTE_PATTERN,
    SYMBOL_DELETIONS,
    SYMBOL_FOLDINGS,
    SYMBOL_SENTINELS,
)


def sql_normalize_expression() -> str:
    """
    Render the four normalization stages as the body of bluecore_normalize().
    """
    fold_from = "".join(SYMBOL_FOLDINGS) + "".join(SYMBOL_DELETIONS)
    fold_to = "".join(SYMBOL_FOLDINGS.values())

    expression = func.regexp_replace(
        literal_column("$1"), PRIME_BETWEEN_DIGITS_PATTERN, " ", "g"
    )
    expression = func.translate(expression, fold_from, fold_to)
    expression = func.regexp_replace(
        expression, SHARP_AFTER_NOTE_PATTERN, f" {SYMBOL_SENTINELS['♯']} ", "g"
    )
    expression = func.regexp_replace(
        expression, FLAT_AFTER_NOTE_PATTERN, f" {SYMBOL_SENTINELS['♭']} ", "g"
    )
    for symbol, sentinel in SYMBOL_SENTINELS.items():
        expression = func.replace(expression, symbol, f" {sentinel} ")
    expression = func.public.f_unaccent(expression)

    # Compiling without a connection, SQLAlchemy cannot ask the server whether
    # backslashes are escapes, so it assumes they are and doubles them. That
    # silently breaks the \w guards in the patterns above: Postgres reads '\\w'
    # as a literal backslash, the lookarounds never match.
    dialect = postgresql.dialect()
    dialect._backslash_escapes = False

    return str(
        expression.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    )


# Pulls the text out of a title, whatever shape it arrived in: a plain string, a
# tagged object like {"@value": "Reader's guide", "@language": "zxx-latn"}, or a
# list of either. Plain ->> hands back raw JSON for the last two, which indexes
# "@value" and the language tag as if they were words in the title.
BLUECORE_JSONB_TEXT = """
                      CREATE
                      OR REPLACE FUNCTION public.bluecore_jsonb_text(value jsonb)
RETURNS text AS $$
                      SELECT CASE jsonb_typeof(value)
                                 WHEN 'string' THEN value #>> '{}'
                                 WHEN 'number' THEN value #>> '{}'
                                 WHEN 'object' THEN value ->> '@value'
                                 WHEN 'array' THEN (SELECT string_agg(public.bluecore_jsonb_text(element), ' ')
                                                    FROM jsonb_array_elements(value) element)
                                 ELSE NULL
                                 END
                                 $$ LANGUAGE sql IMMUTABLE PARALLEL SAFE"""

# jsonb_to_tsv, but reading through bluecore_jsonb_text. Kept separate on purpose:
# jsonb_to_tsv also feeds data_vector, and changing it would rebuild that column
# in production for nothing, since it indexes the whole document anyway.
BLUECORE_TITLES_TO_TSV = """
                      CREATE
                      OR REPLACE FUNCTION public.bluecore_titles_to_tsv(lang_config text, data jsonb, key_name text)
RETURNS tsvector AS $$
                      BEGIN
  IF
                      jsonb_typeof(data) != 'array' THEN
    RETURN to_tsvector(lang_config::regconfig, bluecore_normalize(coalesce(public.bluecore_jsonb_text(data -> key_name), '')));
                      ELSE
    RETURN (
        SELECT to_tsvector(lang_config::regconfig, bluecore_normalize(coalesce(string_agg(public.bluecore_jsonb_text(value -> key_name), ' '), '')))
        FROM jsonb_array_elements(data)
    );
                      END IF;
                      EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'An unexpected error occurred for bluecore_titles_to_tsv: %', SQLERRM;
                      RETURN ''::tsvector;
                      END;
$$
                      LANGUAGE plpgsql IMMUTABLE"""

# Works out the last character (check digit) of an ISBN-10 from its first nine
# digits. Used to turn an ISBN-13 into its matching ISBN-10.
BLUECORE_ISBN10_CHECK_DIGIT = """
CREATE OR REPLACE FUNCTION public.bluecore_isbn10_check_digit(first_nine_digits text)
RETURNS text AS $$
  SELECT CASE WHEN weighted_total % 11 = 10 THEN 'X' ELSE (weighted_total % 11)::text END
  FROM (
    SELECT sum(digit_position * substr(first_nine_digits, digit_position, 1)::int) AS weighted_total
    FROM generate_series(1, 9) AS digit_position
  ) totals
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT"""

# Works out the last character (check digit) of an ISBN-13 from its first
# twelve digits. Used to turn an ISBN-10 into its matching ISBN-13.
BLUECORE_ISBN13_CHECK_DIGIT = """
CREATE OR REPLACE FUNCTION public.bluecore_isbn13_check_digit(first_twelve_digits text)
RETURNS text AS $$
  SELECT ((10 - weighted_total % 10) % 10)::text
  FROM (
    SELECT sum(
      substr(first_twelve_digits, digit_position, 1)::int
      * CASE WHEN digit_position % 2 = 0 THEN 3 ELSE 1 END
    ) AS weighted_total
    FROM generate_series(1, 12) AS digit_position
  ) totals
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT"""

# Cleans up one identifier value so it can be matched exactly, using the rules
# for its scheme. Only ISBN, ISSN, LCCN and DOI are indexed; any other scheme
# returns nothing. Returns a list because a valid ISBN comes back in both its
# 10- and 13-digit forms. The search API runs user input through this same
# function, so what people type and what is stored always line up.
BLUECORE_IDENTIFIER_VALUES = r"""
CREATE OR REPLACE FUNCTION public.bluecore_identifier_values(scheme text, raw_value text)
RETURNS text[] AS $$
DECLARE
  cleaned_value text := btrim(raw_value);
  isbn text;
  lccn_serial_number text;
BEGIN
  IF cleaned_value IS NULL OR cleaned_value = '' THEN
    RETURN '{}';
  END IF;

  IF scheme = 'isbn' THEN
    -- Drop spaces and hyphens, then keep only the leading number, so a
    -- qualifier like "(pbk.)" is ignored. A longer number is left as is.
    cleaned_value := upper(regexp_replace(cleaned_value, '[[:space:]-]', '', 'g'));
    isbn := substring(cleaned_value FROM '^([0-9]{13}|[0-9]{9}[0-9X])(?![0-9X])');
    IF isbn IS NULL THEN
      RETURN ARRAY[cleaned_value];
    END IF;
    -- A valid ISBN-10 also gets its ISBN-13 form, and the other way around
    IF length(isbn) = 10 AND right(isbn, 1) = public.bluecore_isbn10_check_digit(left(isbn, 9)) THEN
      RETURN ARRAY[isbn, '978' || left(isbn, 9) || public.bluecore_isbn13_check_digit('978' || left(isbn, 9))];
    END IF;
    IF left(isbn, 3) = '978' AND right(isbn, 1) = public.bluecore_isbn13_check_digit(left(isbn, 12)) THEN
      RETURN ARRAY[isbn, substr(isbn, 4, 9) || public.bluecore_isbn10_check_digit(substr(isbn, 4, 9))];
    END IF;
    RETURN ARRAY[isbn];
  END IF;

  IF scheme = 'issn' THEN
    -- Drop spaces and the hyphen, then keep only the leading number
    cleaned_value := upper(regexp_replace(cleaned_value, '[[:space:]-]', '', 'g'));
    RETURN ARRAY[coalesce(substring(cleaned_value FROM '^[0-9]{7}[0-9X](?![0-9X])'), cleaned_value)];
  END IF;

  IF scheme = 'lccn' THEN
    -- Library of Congress rules: https://www.loc.gov/marc/lccn-namespace.html
    -- Remove all spaces, and anything from a "/" onward
    cleaned_value := split_part(regexp_replace(cleaned_value, '[[:space:]]', '', 'g'), '/', 1);
    -- "n78-890351" becomes "n78890351", and "85-2" becomes "85000002"
    IF position('-' IN cleaned_value) > 0 THEN
      lccn_serial_number := substr(cleaned_value, position('-' IN cleaned_value) + 1);
      IF lccn_serial_number !~ '^[0-9]{1,6}$' THEN
        RETURN '{}';
      END IF;
      cleaned_value := split_part(cleaned_value, '-', 1) || lpad(lccn_serial_number, 6, '0');
    END IF;
    RETURN array_remove(ARRAY[lower(cleaned_value)], '');
  END IF;

  IF scheme = 'doi' THEN
    -- DOIs ignore case. Drop a leading "https://doi.org/" or "doi:"
    RETURN array_remove(
      ARRAY[regexp_replace(lower(cleaned_value), '^(https?://(dx\.)?doi\.org/|doi:)', '')], '');
  END IF;

  -- Any other scheme (local, upc, ...) is not indexed
  RETURN '{}';
END;
$$ LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE"""

# Collects the ISBNs, ISSNs, LCCNs and DOIs listed in a record's top-level
# identifiedBy and returns them cleaned up, each one twice: with its scheme ("isbn:9780140449112")
# for lookups by scheme, and without it ("9780140449112") for lookups across
# all schemes. This feeds the identifiers column.
#
# The scheme comes from the identifier's @type ("Isbn", "bf:Isbn" or a full
# IRI). A generic "Identifier" type uses its source code instead.
BLUECORE_IDENTIFIERS = """
CREATE OR REPLACE FUNCTION public.bluecore_identifiers(data jsonb)
RETURNS text[] AS $$
  SELECT coalesce(array_agg(DISTINCT token ORDER BY token), '{}')
  FROM (
    SELECT
      lower(coalesce(
        nullif(
          regexp_replace(
            CASE WHEN jsonb_typeof(identifier_node->'@type') = 'array'
                 THEN identifier_node->'@type'->>-1
                 ELSE identifier_node->>'@type' END,
            '^.*[/#:]', ''),
          'Identifier'),
        identifier_node->'source'->>'code')) AS scheme,
      public.bluecore_jsonb_text(identifier_node->'rdf:value') AS raw_value
    FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(data->'identifiedBy') = 'array'
           THEN data->'identifiedBy'
           ELSE jsonb_build_array(data->'identifiedBy') END) AS identifier_node
  ) identifier,
  LATERAL unnest(public.bluecore_identifier_values(identifier.scheme, identifier.raw_value))
    AS normalized_value,
  LATERAL unnest(ARRAY[identifier.scheme || ':' || normalized_value, normalized_value])
    AS token
  WHERE identifier.scheme IS NOT NULL
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE"""

PG_EXT_FUNC: list[str] = [
    "CREATE EXTENSION IF NOT EXISTS unaccent",
    """
CREATE OR REPLACE FUNCTION public.immutable_unaccent(regdictionary, text)
  RETURNS text
  LANGUAGE c IMMUTABLE PARALLEL SAFE STRICT AS
'$libdir/unaccent', 'unaccent_dict'""",
    """
CREATE OR REPLACE FUNCTION public.f_unaccent(text)
  RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
RETURN public.immutable_unaccent(regdictionary 'public.unaccent', $1)""",
    f"""
CREATE OR REPLACE FUNCTION public.bluecore_normalize(text)
  RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
RETURN {sql_normalize_expression()}""",
    """
CREATE OR REPLACE FUNCTION public.jsonb_to_tsv(lang_config text, data jsonb, key_name text)
RETURNS tsvector AS $$
BEGIN
  IF jsonb_typeof(data) != 'array' THEN
    RETURN to_tsvector(lang_config::regconfig, bluecore_normalize(coalesce(data->>key_name, '')));
  ELSE
    RETURN (
        SELECT to_tsvector(lang_config::regconfig, bluecore_normalize(coalesce(string_agg(value->>key_name, ' '), '')))
        FROM jsonb_array_elements(data)
    );
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'An unexpected error occurred for jsonb_to_tsv: %', SQLERRM;
  RETURN ''::tsvector;
END;
$$ LANGUAGE plpgsql IMMUTABLE""",
    # Both are needed here, not just in the migration: the test databases are
    # built from this list, so title_vector cannot be created without them.
    BLUECORE_JSONB_TEXT,
    BLUECORE_TITLES_TO_TSV,
    # Order matters: each function must exist before one that calls it.
    BLUECORE_ISBN10_CHECK_DIGIT,
    BLUECORE_ISBN13_CHECK_DIGIT,
    BLUECORE_IDENTIFIER_VALUES,
    BLUECORE_IDENTIFIERS,
]
