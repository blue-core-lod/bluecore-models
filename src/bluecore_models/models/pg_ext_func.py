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
"""

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
    """
CREATE OR REPLACE FUNCTION public.jsonb_to_tsv(lang_config text, data jsonb, key_name text) 
RETURNS tsvector AS $$
BEGIN
  IF jsonb_typeof(data) != 'array' THEN
    RETURN to_tsvector(lang_config::regconfig, f_unaccent(coalesce(data->>key_name, '')));
  ELSE
    RETURN (
        SELECT to_tsvector(lang_config::regconfig, f_unaccent(coalesce(string_agg(value->>key_name, ' '), '')))
        FROM jsonb_array_elements(data)
    );
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'An unexpected error occurred for jsonb_to_tsv: %', SQLERRM;
  RETURN ''::tsvector;
END;
$$ LANGUAGE plpgsql IMMUTABLE""",
]
