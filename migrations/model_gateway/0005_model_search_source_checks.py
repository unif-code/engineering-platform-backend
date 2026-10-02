"""Persist only typed Responses source evidence; old facts and fingerprints are unchanged."""

from alembic import op

revision = "0005_model_search_sources"
down_revision = "0004_model_thinking_checks"
branch_labels = None
depends_on = None

_COLUMNS = (
    "observed_search_calls",
    "observed_source_signal",
    "provider_search_call_count",
    "search_sources",
    "search_queries",
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT connection_check_check_kind_check"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT connection_check_check_kind_check
        CHECK (check_kind IN ('BASIC_TEXT','STREAM_TEXT','STREAM_STOP','THINKING','SEARCH_SOURCES'))
    """)
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN observed_search_calls INTEGER CHECK (observed_search_calls BETWEEN 0 AND 8)"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check ADD COLUMN observed_source_signal BOOLEAN"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD COLUMN provider_search_call_count INTEGER CHECK (provider_search_call_count >= 0)"
    )
    op.execute("ALTER TABLE model_gateway.connection_check ADD COLUMN search_sources JSONB")
    op.execute("ALTER TABLE model_gateway.connection_check ADD COLUMN search_queries JSONB")
    op.execute("""
        CREATE FUNCTION model_gateway.valid_search_evidence(sources JSONB, queries JSONB)
        RETURNS BOOLEAN LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE item JSONB; call_ids TEXT[] := '{}'; source_keys TEXT[] := '{}';
                source_key TEXT;
        BEGIN
            IF jsonb_typeof(sources) IS DISTINCT FROM 'array'
               OR jsonb_typeof(queries) IS DISTINCT FROM 'array'
            THEN RETURN false; END IF;
            IF jsonb_array_length(sources)>32 OR jsonb_array_length(queries)>8
            THEN RETURN false; END IF;
            FOR item IN SELECT value FROM jsonb_array_elements(queries) LOOP
                IF jsonb_typeof(item) IS DISTINCT FROM 'object'
                   OR item-ARRAY['call_id','count','digest'] <> '{}'::jsonb
                   OR NOT (item ?& ARRAY['call_id','count','digest'])
                   OR jsonb_typeof(item->'call_id') IS DISTINCT FROM 'string'
                   OR (item->>'call_id') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
                   OR (item->>'call_id')=ANY(call_ids)
                THEN RETURN false; END IF;
                IF NOT ((item->'count'='null'::jsonb AND item->'digest'='null'::jsonb)
                    OR (jsonb_typeof(item->'count')='number'
                        AND (item->>'count') ~ '^[0-8]$'
                        AND jsonb_typeof(item->'digest')='string'
                        AND (item->>'digest') ~ '^[0-9a-f]{64}$'))
                THEN RETURN false; END IF;
                call_ids := array_append(call_ids, item->>'call_id');
            END LOOP;
            FOR item IN SELECT value FROM jsonb_array_elements(sources) LOOP
                IF jsonb_typeof(item) IS DISTINCT FROM 'object'
                   OR item-ARRAY['call_id','sanitized_url'] <> '{}'::jsonb
                   OR NOT (item ?& ARRAY['call_id','sanitized_url'])
                   OR jsonb_typeof(item->'call_id') IS DISTINCT FROM 'string'
                   OR NOT ((item->>'call_id')=ANY(call_ids))
                   OR jsonb_typeof(item->'sanitized_url') IS DISTINCT FROM 'string'
                   OR length(item->>'sanitized_url')>2048
                   OR (item->>'sanitized_url') !~ '^https://[^/?#@[:space:]]+(/[^?#[:cntrl:]]*)?$'
                THEN RETURN false; END IF;
                source_key := (item->>'call_id') || ' ' || (item->>'sanitized_url');
                IF source_key=ANY(source_keys) THEN RETURN false; END IF;
                source_keys := array_append(source_keys, source_key);
            END LOOP;
            RETURN true;
        END $$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION model_gateway.valid_search_evidence(JSONB,JSONB) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION model_gateway.valid_search_evidence(JSONB,JSONB) "
        "TO model_gateway_rw,model_gateway_worker_rw"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observation_shape"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT ck_model_check_observation_shape
        CHECK (((
            (observed_bytes IS NULL AND observed_text IS NULL AND observed_normal_completion IS NULL
             AND observed_data_events IS NULL AND observed_text_deltas IS NULL
             AND observed_text_bytes IS NULL
             AND observed_completion_marker IS NULL AND observed_local_closed IS NULL
             AND provider_cancellation IS NULL
             AND observed_reasoning IS NULL AND observed_reasoning_deltas IS NULL
             AND observed_reasoning_bytes IS NULL)
            OR (state IN ('SUCCEEDED','FAILED','UNKNOWN') AND observed_bytes IS NOT NULL
                AND observed_text IS NOT NULL AND observed_normal_completion IS NOT NULL AND (
                  (check_kind='BASIC_TEXT' AND observed_data_events IS NULL
                   AND observed_text_deltas IS NULL
                   AND observed_text_bytes IS NULL AND observed_completion_marker IS NULL
                   AND observed_local_closed IS NULL AND provider_cancellation IS NULL
                   AND observed_reasoning IS NULL AND observed_reasoning_deltas IS NULL
                   AND observed_reasoning_bytes IS NULL)
                  OR
                  (check_kind IN ('STREAM_TEXT','STREAM_STOP','THINKING')
                   AND observed_data_events IS NOT NULL
                   AND observed_text_deltas IS NOT NULL AND observed_text_bytes IS NOT NULL
                   AND observed_completion_marker IS NOT NULL AND observed_local_closed IS NOT NULL
                   AND provider_cancellation IS NOT NULL AND provider_cancellation='UNCONFIRMED'
                   AND ((check_kind='THINKING' AND observed_reasoning IS NOT NULL
                         AND observed_reasoning_deltas IS NOT NULL
                         AND observed_reasoning_bytes IS NOT NULL)
                        OR (check_kind<>'THINKING' AND observed_reasoning IS NULL
                            AND observed_reasoning_deltas IS NULL
                            AND observed_reasoning_bytes IS NULL)))
                ))
)
            AND observed_search_calls IS NULL AND observed_source_signal IS NULL
            AND provider_search_call_count IS NULL
            AND search_sources IS NULL AND search_queries IS NULL)
            OR (check_kind='SEARCH_SOURCES' AND state IN ('SUCCEEDED','FAILED','UNKNOWN')
                AND observed_bytes IS NOT NULL AND observed_text IS NOT NULL
                AND observed_normal_completion IS NOT NULL AND observed_local_closed IS NOT NULL
                AND provider_cancellation IS NOT NULL AND provider_cancellation='UNCONFIRMED'
                AND observed_data_events IS NULL AND observed_text_deltas IS NULL
                AND observed_text_bytes IS NULL AND observed_completion_marker IS NULL
                AND observed_reasoning IS NULL AND observed_reasoning_deltas IS NULL
                AND observed_reasoning_bytes IS NULL
                AND observed_search_calls IS NOT NULL AND observed_source_signal IS NOT NULL
                AND model_gateway.valid_search_evidence(search_sources,search_queries)
                AND observed_search_calls=jsonb_array_length(search_queries)
                AND observed_source_signal=(jsonb_array_length(search_sources)>0)
            ))
    """)
    op.execute(
        "GRANT UPDATE ("
        + ",".join(_COLUMNS)
        + ") ON model_gateway.connection_check TO model_gateway_worker_rw"
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM model_gateway.connection_check WHERE check_kind='SEARCH_SOURCES')
            THEN RAISE EXCEPTION 'search source facts prevent downgrade'; END IF;
        END $$
    """)
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observation_shape"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT ck_model_check_observation_shape
        CHECK (
            (observed_bytes IS NULL AND observed_text IS NULL AND observed_normal_completion IS NULL
             AND observed_data_events IS NULL AND observed_text_deltas IS NULL
             AND observed_text_bytes IS NULL
             AND observed_completion_marker IS NULL AND observed_local_closed IS NULL
             AND provider_cancellation IS NULL
             AND observed_reasoning IS NULL AND observed_reasoning_deltas IS NULL
             AND observed_reasoning_bytes IS NULL)
            OR (state IN ('SUCCEEDED','FAILED','UNKNOWN') AND observed_bytes IS NOT NULL
                AND observed_text IS NOT NULL AND observed_normal_completion IS NOT NULL AND (
                  (check_kind='BASIC_TEXT' AND observed_data_events IS NULL
                   AND observed_text_deltas IS NULL
                   AND observed_text_bytes IS NULL AND observed_completion_marker IS NULL
                   AND observed_local_closed IS NULL AND provider_cancellation IS NULL
                   AND observed_reasoning IS NULL AND observed_reasoning_deltas IS NULL
                   AND observed_reasoning_bytes IS NULL)
                  OR
                  (check_kind IN ('STREAM_TEXT','STREAM_STOP','THINKING')
                   AND observed_data_events IS NOT NULL
                   AND observed_text_deltas IS NOT NULL AND observed_text_bytes IS NOT NULL
                   AND observed_completion_marker IS NOT NULL AND observed_local_closed IS NOT NULL
                   AND provider_cancellation IS NOT NULL AND provider_cancellation='UNCONFIRMED'
                   AND ((check_kind='THINKING' AND observed_reasoning IS NOT NULL
                         AND observed_reasoning_deltas IS NOT NULL
                         AND observed_reasoning_bytes IS NOT NULL)
                        OR (check_kind<>'THINKING' AND observed_reasoning IS NULL
                            AND observed_reasoning_deltas IS NULL
                            AND observed_reasoning_bytes IS NULL)))
                ))
        )
    """)
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT connection_check_check_kind_check"
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT connection_check_check_kind_check
        CHECK (check_kind IN ('BASIC_TEXT','STREAM_TEXT','STREAM_STOP','THINKING'))
    """)
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE model_gateway.connection_check DROP COLUMN {column}")
    op.execute("DROP FUNCTION model_gateway.valid_search_evidence(JSONB,JSONB)")
