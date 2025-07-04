"""Add JSONB indexes for works, instances, other_resources

Revision ID: 20250701
Revises: 3be9942c0ebe
Create Date: 2025-07-02
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20250701"
down_revision: str = "3be9942c0ebe"
branch_labels = None
depends_on = None


def upgrade():
    ####################################
    ##  Create the pg_trgm extension  ##
    ####################################
    # For trigram indexing for iLike and pattern matching
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    ##################----------------------------------------------------------
    ##  ALL MODELS  ##
    ##################
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_mainTitle
            ON resource_base ((data -> 'title' ->> 'mainTitle'));
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_text_trgm
            ON resource_base
            USING gin ((data ::text) gin_trgm_ops);
        """
    )

    # GIN TRGM: Pattern match (LIKE/ILIKE) on mainTitle inside title
    # Use: WHERE data -> 'title' ->> 'mainTitle' ILIKE '%Cat%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_mainTitle_trgm
        ON resource_base
        USING gin ((data -> 'title' ->> 'mainTitle') gin_trgm_ops)
        """
    )

    # BTREE: Exact match on derivedFrom @id field
    # Use: WHERE data -> 'derivedFrom' ->> '@id' = 'http://id.loc.gov/resources/instances/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_derivedFrom_id
            ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        """
    )

    # BTREE: Exact match on native UUID
    # Use: WHERE uuid = 'abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_uuid
            ON resource_base (uuid)
        """
    )

    # GIN: Fallback index for containment search on any key
    # Use: WHERE data @> '{"title": {"mainTitle": "Le mal joli"}}'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_gin
        ON resource_base
        USING gin (data)
        """
    )

    #############---------------------------------------------------------------
    ##  WORKS  ##
    #############

    # GIN TRGM: Fallback keyword pattern match across entire JSONB payload
    # Use: WHERE data::text ILIKE '%keyword%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_text_trgm
            ON resource_base
            USING gin ((data ::text) gin_trgm_ops)
            WHERE type = 'works'
        """
    )

    # BTREE: Fast exact match on RDF @id (Work URI)
    # Use: WHERE data ->> '@id' = 'https://bcld.info/works/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_id
        ON resource_base ((data ->> '@id'))
        WHERE type = 'works'
        """
    )

    # BTREE: Exact match if @type is scalar (fallback only)
    # Use: WHERE data ->> '@type' = 'Work'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_type
        ON resource_base ((data ->> '@type'))
        WHERE type = 'works'
        """
    )

    # BTREE: Filter or facet on language field
    # Use: WHERE data -> 'language' ->> '@id' = 'http://id.loc.gov/vocabulary/languages/eng'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_language
        ON resource_base ((data -> 'language' ->> '@id'))
        WHERE type = 'works'
        """
    )

    # GIN JSONPath: Proper index for RDF array @type
    # Use: WHERE jsonb_path_exists(data, '$.\"@type\"[*] ? (@ == \"Work\")')
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_type_path
        ON resource_base
        USING gin (jsonb_path_query_array(data, '$.\"@type\"[*]'))
        WHERE type = 'works'
        """
    )

    # GIN JSONPath: Fast match on contribution agents inside arrays
    # Use: WHERE jsonb_path_exists(data, '$.contribution[*].agent ? (@.\"@id\" == \"http://id.loc.gov/rwo/agents/no2012149645\")')
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_contribution_agent
        ON resource_base
        USING gin (jsonb_path_query_array(data, '$.contribution[*].agent'))
        WHERE type = 'works'
        """
    )

    # GIN: Fallback index for containment search on any key
    # Use: WHERE data @> '{"title": {"mainTitle": "Le mal joli"}}'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_gin
        ON resource_base
        USING gin (data)
        WHERE type = 'works'
        """
    )

    # BTREE: Exact match on native UUID
    # Use: WHERE uuid = 'abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_uuid
        ON resource_base (uuid)
        WHERE type = 'works'
        """
    )

    # BTREE: Exact match on native URI column
    # Use: WHERE uri = 'https://bcld.info/works/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_uri
        ON resource_base (uri)
        WHERE type = 'works'
        """
    )

    # BTREE: Filter or sort by created_at timestamp
    # Use: WHERE created_at >= '2025-01-01'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_created_at
        ON resource_base (created_at)
        WHERE type = 'works'
        """
    )

    # BTREE: Filter or sort by updated_at timestamp
    # Use: WHERE updated_at >= '2025-01-01'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_updated_at
        ON resource_base (updated_at)
        WHERE type = 'works'
        """
    )


    # BTREE: Fast exact match on mainTitle inside title block
    # Use: WHERE data -> 'title' ->> 'mainTitle' = 'Example Title'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_mainTitle
        ON resource_base ((data -> 'title' ->> 'mainTitle'))
        WHERE type = 'works'
        """
    )

    # GIN TRGM: Pattern match (LIKE/ILIKE) on mainTitle inside title
    # Use: WHERE data -> 'title' ->> 'mainTitle' ILIKE '%Cat%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_mainTitle_trgm
        ON resource_base
        USING gin ((data -> 'title' ->> 'mainTitle') gin_trgm_ops)
        WHERE type = 'works'
        """
    )


    # BTREE: Exact match on derivedFrom @id field
    # Use: WHERE data -> 'derivedFrom' ->> '@id' = 'http://id.loc.gov/resources/instances/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_derivedFrom_id
        ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        WHERE type = 'works'
        """
    )

    #################-----------------------------------------------------------
    ##  INSTANCES  ##
    #################

    # GIN TRGM: Fallback keyword pattern match across entire JSONB payload
    # Use: WHERE data::text ILIKE '%keyword%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_text_trgm
            ON resource_base
            USING gin ((data ::text) gin_trgm_ops)
            WHERE type = 'instances'
        """
    )

    # BTREE: Fast exact match on RDF @id (Instance URI)
    # Use: WHERE data ->> '@id' = 'https://bcld.info/instances/xyz789'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_id
        ON resource_base ((data ->> '@id'))
        WHERE type = 'instances'
        """
    )

    # BTREE: Exact match if @type is scalar (rare, fallback only)
    # Use: WHERE data ->> '@type' = 'Instance'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_type
        ON resource_base ((data ->> '@type'))
        WHERE type = 'instances'
        """
    )

    # BTREE: Filter or facet on carrier type
    # Use: WHERE data -> 'carrier' ->> '@id' = 'http://id.loc.gov/vocabulary/carriers/cr'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_carrier
        ON resource_base ((data -> 'carrier' ->> '@id'))
        WHERE type = 'instances'
        """
    )

    # GIN JSONPath: Proper index for RDF array @type
    # Use: WHERE jsonb_path_exists(data, '$.\"@type\"[*] ? (@ == \"Instance\")')
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_type_path
        ON resource_base
        USING gin (jsonb_path_query_array(data, '$.\"@type\"[*]'))
        WHERE type = 'instances'
        """
    )

    # GIN JSONPath: Index for matching any identifier (ISBN, LCCN, etc.)
    # Use: WHERE jsonb_path_exists(data, '$.identifiedBy[*] ? (@.\"@type\" == \"Isbn\")')
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_identified_by
        ON resource_base
        USING gin (jsonb_path_query_array(data, '$.identifiedBy[*]'))
        WHERE type = 'instances'
        """
    )

    # GIN: Fallback containment index
    # Use: WHERE data @> '{"title": {"mainTitle": "[Electronic resource]"}}'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_gin
        ON resource_base
        USING gin (data)
        WHERE type = 'instances'
        """
    )

    # BTREE: Exact match on native UUID
    # Use: WHERE uuid = 'abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_uuid
        ON resource_base (uuid)
        WHERE type = 'instances'
        """
    )

    # BTREE: Exact match on native URI column
    # Use: WHERE uri = 'https://bcld.info/instances/xyz789'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_uri
        ON resource_base (uri)
        WHERE type = 'instances'
        """
    )

    # BTREE: Filter or sort by created_at timestamp
    # Use: WHERE created_at >= '2025-01-01'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_created_at
        ON resource_base (created_at)
        WHERE type = 'instances'
        """
    )

    # BTREE: Filter or sort by updated_at timestamp
    # Use: WHERE updated_at >= '2025-01-01'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_updated_at
        ON resource_base (updated_at)
        WHERE type = 'instances'
        """
    )

    # BTREE: Fast exact match on mainTitle inside title block
    # Use: WHERE data -> 'title' ->> 'mainTitle' = 'Example Title'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_mainTitle
        ON resource_base ((data -> 'title' ->> 'mainTitle'))
        WHERE type = 'instances'
        """
    )

    # GIN TRGM: Pattern match (LIKE/ILIKE) on mainTitle inside title block
    # Use: WHERE data -> 'title' ->> 'mainTitle' ILIKE '%Cat%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_mainTitle_trgm
        ON resource_base
        USING gin ((data -> 'title' ->> 'mainTitle') gin_trgm_ops)
        WHERE type = 'instances'
        """
    )

    # BTREE: Exact match on derivedFrom @id field
    # Use: WHERE data -> 'derivedFrom' ->> '@id' = 'http://id.loc.gov/resources/instances/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_derivedFrom_id
        ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        WHERE type = 'instances'
        """
    )

    #######################-----------------------------------------------------
    ##  OTHER_RESOURCES  ##
    #######################

    # GIN TRGM: Fallback keyword pattern match across entire JSONB payload
    # Use: WHERE data::text ILIKE '%keyword%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_text_trgm
            ON resource_base
            USING gin ((data ::text) gin_trgm_ops)
            WHERE type = 'other_resources'
        """
    )

    # BTREE: Exact match on RDF @id (Other Resource URI)
    # Use: WHERE data ->> '@id' = 'http://id.loc.gov/vocabulary/countries/dr'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_id
        ON resource_base ((data ->> '@id'))
        WHERE type = 'other_resources'
        """
    )

    # BTREE: Fallback exact match for scalar @type (rare)
    # Use: WHERE data ->> '@type' = 'Place'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_type
        ON resource_base ((data ->> '@type'))
        WHERE type = 'other_resources'
        """
    )

    # GIN JSONPath: Proper index for RDF array @type
    # Use: WHERE jsonb_path_exists(data, '$.\"@type\"[*] ? (@ == \"http://id.loc.gov/ontologies/bibframe/Place\")')
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_type_path
        ON resource_base
        USING gin (jsonb_path_query_array(data, '$.\"@type\"[*]'))
        WHERE type = 'other_resources'
        """
    )

    # GIN: Fallback containment index
    # Use: WHERE data @> '{"http://id.loc.gov/ontologies/bibframe/code": [{"@value": "dr"}]}'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_gin
        ON resource_base
        USING gin (data)
        WHERE type = 'other_resources'
        """
    )

    # BTREE: Exact match on native UUID
    # Use: WHERE uuid = 'abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_uuid
        ON resource_base (uuid)
        WHERE type = 'other_resources'
        """
    )

    # BTREE: Exact match on native URI column
    # Use: WHERE uri = 'http://id.loc.gov/vocabulary/countries/dr'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_uri
        ON resource_base (uri)
        WHERE type = 'other_resources'
        """
    )

    # BTREE: Filter or sort by created_at timestamp
    # Use: WHERE created_at >= '2025-01-01'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_created_at
        ON resource_base (created_at)
        WHERE type = 'other_resources'
        """
    )

    # BTREE: Filter or sort by updated_at timestamp
    # Use: WHERE updated_at >= '2025-01-01'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_updated_at
        ON resource_base (updated_at)
        WHERE type = 'other_resources'
        """
    )

    # BTREE: Fast exact match on mainTitle inside title block
    # Use: WHERE data -> 'title' ->> 'mainTitle' = 'Example Title'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_mainTitle
        ON resource_base ((data -> 'title' ->> 'mainTitle'))
        WHERE type = 'other_resources'
        """
    )

    # GIN TRGM: Pattern match (LIKE/ILIKE) on mainTitle inside title block
    # Use: WHERE data -> 'title' ->> 'mainTitle' ILIKE '%Cat%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_mainTitle_trgm
        ON resource_base
        USING gin ((data -> 'title' ->> 'mainTitle') gin_trgm_ops)
        WHERE type = 'other_resources'
        """
    )


    # BTREE: Exact match on derivedFrom @id field
    # Use: WHERE data -> 'derivedFrom' ->> '@id' = 'http://id.loc.gov/resources/instances/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_other_resources_on_data_derivedFrom_id
        ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        WHERE type = 'other_resources'
        """
    )


def downgrade():
    # Drop shared global indexes (non-scoped)
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_mainTitle")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_text_trgm")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_mainTitle_trgm")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_derivedFrom_id")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_uuid")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_gin")

    op.execute("DROP INDEX IF EXISTS index_works_on_data_text_trgm")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_text_trgm")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_text_trgm")

    op.execute("DROP INDEX IF EXISTS index_works_on_data_mainTitle_trgm")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_mainTitle_trgm")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_mainTitle_trgm")

    op.execute("DROP INDEX IF EXISTS index_works_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_type")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_language")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_type_path")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_contribution_agent")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_gin")
    op.execute("DROP INDEX IF EXISTS index_works_on_uuid")
    op.execute("DROP INDEX IF EXISTS index_works_on_uri")
    op.execute("DROP INDEX IF EXISTS index_works_on_created_at")
    op.execute("DROP INDEX IF EXISTS index_works_on_updated_at")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_mainTitle")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_derivedFrom_id")

    op.execute("DROP INDEX IF EXISTS index_instances_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_type")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_carrier")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_type_path")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_identified_by")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_gin")
    op.execute("DROP INDEX IF EXISTS index_instances_on_uuid")
    op.execute("DROP INDEX IF EXISTS index_instances_on_uri")
    op.execute("DROP INDEX IF EXISTS index_instances_on_created_at")
    op.execute("DROP INDEX IF EXISTS index_instances_on_updated_at")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_mainTitle")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_derivedFrom_id")

    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_type")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_type_path")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_gin")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_uuid")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_uri")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_created_at")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_updated_at")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_mainTitle")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_derivedFrom_id")
