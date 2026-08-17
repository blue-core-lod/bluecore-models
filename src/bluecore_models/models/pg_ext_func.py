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
    PRIME_BETWEEN_DIGITS_PATTERN,
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
    for symbol, sentinel in SYMBOL_SENTINELS.items():
        expression = func.replace(expression, symbol, f" {sentinel} ")
    expression = func.public.f_unaccent(expression)

    return str(
        expression.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


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
]
