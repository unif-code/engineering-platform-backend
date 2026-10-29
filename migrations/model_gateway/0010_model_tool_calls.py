"""Persist bounded TOOL_CALL observations and six-kind DECLARED dossier references."""

from alembic import op

revision = "0010_model_tool_calls"
down_revision = "0009_model_check_deadline"
branch_labels = None
depends_on = None

# The previous shape remains exact for old kinds; new columns must be absent there.
_LEGACY_SHAPE = """((
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
            )"""
_DOSSIER_VALIDATOR = """
        CREATE OR REPLACE FUNCTION model_gateway.valid_dossier_snapshot(p JSONB) RETURNS BOOLEAN
        LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE item JSONB; field TEXT; n INTEGER;
        BEGIN
            IF (jsonb_typeof(p)='object' AND
                p ?& ARRAY['id','deployment_id','candidate_revision','created_by','created_at',
                    'materials','checks'] AND
                p - ARRAY['id','deployment_id','candidate_revision','created_by','created_at',
                    'materials','checks']='{}'::jsonb AND
                jsonb_typeof(p->'materials')='array' AND jsonb_typeof(p->'checks')='array') IS NOT
                    TRUE
            THEN RETURN FALSE; END IF;
            IF jsonb_array_length(p->'materials')>32 OR jsonb_array_length(p->'checks')>5 OR
               jsonb_array_length(p->'materials')+jsonb_array_length(p->'checks')=0
            THEN RETURN FALSE; END IF;
            FOR item IN SELECT value FROM jsonb_array_elements(p->'materials') LOOP
                IF (jsonb_typeof(item)='object' AND
                    item ?& ARRAY['category','title','source_reference','external_version',
                        'declared_content_sha256','expires_at','note','provenance'] AND
                    item - ARRAY['category','title','source_reference','external_version',
                        'declared_content_sha256','expires_at','note','provenance']='{}'::jsonb AND
                    item->>'provenance'='DECLARED' AND
                    item->>'category' IN ('MODEL_IDENTITY','CAPABILITY_SPEC','CONTEXT_LIMITS',
                        'PARAMETER_SCHEMA','PRICING','QUOTA','DATA_PROCESSING','HEALTH') AND
                    jsonb_typeof(item->'title')='string' AND length(btrim(item->>'title')) BETWEEN
                        1 AND 120 AND
                    jsonb_typeof(item->'source_reference')='string' AND
                        length(item->>'source_reference') BETWEEN 5 AND 2048 AND
                    item->>'source_reference' ~ ('^(https://[^/?#@[:space:]]+[^?#[:space:]]*|' ||
                     'urn:[A-Za-z0-9][A-Za-z0-9-]{0,31}:[^?#[:space:]]+)$')) IS NOT TRUE
                THEN RETURN FALSE; END IF;
                FOR field,n IN VALUES ('external_version',128),('note',500) LOOP
                    IF item->field <> 'null'::jsonb AND
                       (jsonb_typeof(item->field)='string' AND length(btrim(item->>field)) BETWEEN
                           1 AND n) IS NOT TRUE
                    THEN RETURN FALSE; END IF;
                END LOOP;
                IF item->'declared_content_sha256' <> 'null'::jsonb AND
                   (jsonb_typeof(item->'declared_content_sha256')='string' AND
                       item->>'declared_content_sha256' ~ '^[0-9a-f]{64}$') IS NOT TRUE
                THEN RETURN FALSE; END IF;
                IF item->'expires_at' <> 'null'::jsonb THEN
                    IF jsonb_typeof(item->'expires_at')<>'string' THEN RETURN FALSE; END IF;
                    PERFORM (item->>'expires_at')::timestamptz;
                END IF;
            END LOOP;
            FOR item IN SELECT value FROM jsonb_array_elements(p->'checks') LOOP
                IF (jsonb_typeof(item)='object' AND
                    item ?& ARRAY['check_id','check_revision','check_kind','input_digest',
                        'result_summary','result_hash'] AND
                    item - ARRAY['check_id','check_revision','check_kind','input_digest',
                        'result_summary','result_hash']='{}'::jsonb AND
                    jsonb_typeof(item->'check_id')='string' AND
                        jsonb_typeof(item->'check_revision')='number' AND
                    item->>'check_revision' ~ '^[1-9][0-9]*$' AND
                    item->>'check_kind' IN ('BASIC_TEXT','STREAM_TEXT','STREAM_STOP','THINKING',
                        'SEARCH_SOURCES') AND
                    item->>'input_digest' ~ '^[0-9a-f]{64}$' AND item->>'result_hash' ~
                        '^[0-9a-f]{64}$' AND
                    jsonb_typeof(item->'result_summary')='object' AND
                    (item->'result_summary') ?& ARRAY['state','reason','finished_at',
                        'reported_model_id','provider_request_id','elapsed_ms','usage'] AND
                    (item->'result_summary') - ARRAY['state','reason','finished_at',
                        'reported_model_id','provider_request_id','elapsed_ms',
                        'usage']='{}'::jsonb AND
                    item->'result_summary'->>'state' IN ('SUCCEEDED','FAILED','UNKNOWN',
                        'BLOCKED')) IS NOT TRUE
                THEN RETURN FALSE; END IF;
                PERFORM (item->>'check_id')::uuid;
            END LOOP;
            IF EXISTS (SELECT 1 FROM jsonb_array_elements(p->'checks') r GROUP BY r->>'check_kind'
                HAVING count(*)>1) OR
               EXISTS (SELECT 1 FROM jsonb_array_elements(p->'checks') r GROUP BY r->>'check_id'
                   HAVING count(*)>1)
            THEN RETURN FALSE; END IF;
            RETURN TRUE;
        EXCEPTION WHEN OTHERS THEN RETURN FALSE;
        END $$
    """
_COLUMNS = ("observed_matching_call", "observed_tool_call_completion")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT connection_check_check_kind_check"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD CONSTRAINT connection_check_check_kind_check CHECK (check_kind IN "
        "('BASIC_TEXT','STREAM_TEXT','STREAM_STOP','THINKING','SEARCH_SOURCES','TOOL_CALL'))"
    )
    for name in _COLUMNS:
        op.execute(f"ALTER TABLE model_gateway.connection_check ADD COLUMN {name} BOOLEAN")
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observation_shape"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD CONSTRAINT ck_model_check_observation_shape CHECK (("
        + _LEGACY_SHAPE
        + """
               ) AND observed_matching_call IS NULL AND observed_tool_call_completion IS NULL
               OR (check_kind='TOOL_CALL' AND state IN ('SUCCEEDED','FAILED','UNKNOWN')
                   AND observed_bytes IS NOT NULL AND observed_local_closed IS NOT NULL
                   AND observed_matching_call IS NOT NULL
                   AND observed_tool_call_completion IS NOT NULL
                   AND observed_text IS NULL AND observed_normal_completion IS NULL
                   AND observed_data_events IS NULL AND observed_text_deltas IS NULL
                   AND observed_text_bytes IS NULL AND observed_completion_marker IS NULL
                   AND provider_cancellation IS NULL AND observed_reasoning IS NULL
                   AND observed_reasoning_deltas IS NULL AND observed_reasoning_bytes IS NULL
                   AND observed_search_calls IS NULL AND observed_source_signal IS NULL
                   AND provider_search_call_count IS NULL AND search_sources IS NULL
                   AND search_queries IS NULL))
    """
    )
    op.execute("""
        ALTER TABLE model_gateway.connection_check ADD CONSTRAINT ck_model_check_tool_success
        CHECK (check_kind<>'TOOL_CALL' OR state<>'SUCCEEDED' OR
               (observed_matching_call IS TRUE AND observed_tool_call_completion IS TRUE
                AND observed_local_closed IS TRUE))
    """)
    op.execute(
        "GRANT UPDATE (" + ",".join(_COLUMNS) + ") "
        "ON model_gateway.connection_check TO model_gateway_worker_rw"
    )
    op.execute(
        _DOSSIER_VALIDATOR.replace(
            "jsonb_array_length(p->'checks')>5", "jsonb_array_length(p->'checks')>6"
        ).replace("'SEARCH_SOURCES') AND", "'SEARCH_SOURCES','TOOL_CALL') AND")
    )


def downgrade() -> None:
    # No rows, receipts or immutable snapshots are deleted or rewritten to permit rollback.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM model_gateway.connection_check WHERE check_kind='TOOL_CALL')
               OR EXISTS (SELECT FROM model_gateway.validation_dossier
                   WHERE jsonb_array_length(snapshot_text::jsonb->'checks')>5
                      OR EXISTS (SELECT FROM jsonb_array_elements(snapshot_text::jsonb->'checks') r
                                 WHERE r->>'check_kind'='TOOL_CALL'))
            THEN RAISE EXCEPTION 'tool-call facts prevent downgrade'; END IF;
        END $$
    """)
    op.execute(_DOSSIER_VALIDATOR)
    op.execute(
        "ALTER TABLE model_gateway.connection_check DROP CONSTRAINT ck_model_check_tool_success"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT ck_model_check_observation_shape"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD CONSTRAINT ck_model_check_observation_shape CHECK (" + _LEGACY_SHAPE + ")"
    )
    for name in _COLUMNS:
        op.execute(f"ALTER TABLE model_gateway.connection_check DROP COLUMN {name}")
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "DROP CONSTRAINT connection_check_check_kind_check"
    )
    op.execute(
        "ALTER TABLE model_gateway.connection_check "
        "ADD CONSTRAINT connection_check_check_kind_check CHECK (check_kind IN "
        "('BASIC_TEXT','STREAM_TEXT','STREAM_STOP','THINKING','SEARCH_SOURCES'))"
    )
