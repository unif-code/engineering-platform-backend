"""Append-only declared material dossiers, never effective Model policy."""

from alembic import op

revision = "0006_model_validation_dossiers"
down_revision = "0005_model_search_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE FUNCTION model_gateway.valid_dossier_snapshot(p JSONB) RETURNS BOOLEAN
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
    """)
    op.execute("""
        CREATE TABLE model_gateway.validation_dossier (
            id UUID PRIMARY KEY,
            deployment_id UUID NOT NULL REFERENCES model_gateway.deployment(id),
            candidate_revision INTEGER NOT NULL CHECK (candidate_revision>=1),
            created_by TEXT NOT NULL CHECK (length(created_by)>0),
            created_at TIMESTAMPTZ NOT NULL,
            snapshot_text TEXT NOT NULL CHECK (octet_length(snapshot_text)<=524288),
            snapshot_hash TEXT NOT NULL CHECK (
                snapshot_hash ~ '^[0-9a-f]{64}$' AND
                snapshot_hash=encode(sha256(convert_to(snapshot_text,'UTF8')),'hex')),
            CHECK (model_gateway.valid_dossier_snapshot(snapshot_text::jsonb))
        )
    """)
    op.execute("""
        CREATE FUNCTION model_gateway.guard_validation_dossier() RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        DECLARE p JSONB; ref JSONB; candidate model_gateway.deployment; fact
            model_gateway.connection_check;
        BEGIN
            IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'validation dossiers are immutable'; END IF;
            p := NEW.snapshot_text::jsonb;
            IF NOT model_gateway.valid_dossier_snapshot(p) THEN RAISE EXCEPTION
                'invalid dossier snapshot'; END IF;
            IF ((p->>'id')::uuid,(p->>'deployment_id')::uuid,(p->>'candidate_revision')::integer,
                 p->>'created_by',(p->>'created_at')::timestamptz) IS DISTINCT FROM
                (NEW.id,NEW.deployment_id,NEW.candidate_revision,NEW.created_by,NEW.created_at)
            THEN RAISE EXCEPTION 'dossier identity mismatch'; END IF;
            SELECT * INTO candidate FROM model_gateway.deployment WHERE id=NEW.deployment_id FOR
                UPDATE;
            IF NOT FOUND OR candidate.state<>'DRAFT' OR candidate.revision<>NEW.candidate_revision
            THEN RAISE EXCEPTION 'dossier candidate changed'; END IF;
            FOR ref IN SELECT value FROM jsonb_array_elements(p->'checks') ORDER BY
                value->>'check_id' LOOP
                SELECT * INTO fact FROM model_gateway.connection_check WHERE
                    id=(ref->>'check_id')::uuid FOR UPDATE;
                IF NOT FOUND OR fact.deployment_id<>NEW.deployment_id OR
                    (fact.input->>'deployment_revision')::integer<>NEW.candidate_revision OR
                    fact.state NOT IN ('SUCCEEDED','FAILED','UNKNOWN','BLOCKED') OR
                    fact.revision<>(ref->>'check_revision')::integer OR
                        fact.check_kind<>ref->>'check_kind' OR
                    fact.input->>'input_digest'<>ref->>'input_digest' OR
                    (ref->'result_summary') - 'finished_at' IS DISTINCT FROM jsonb_build_object(
                        'state',fact.state,'reason',fact.reason,'reported_model_id',
                            fact.reported_model_id,
                        'provider_request_id',fact.provider_request_id,'elapsed_ms',
                            fact.elapsed_ms,'usage',fact.usage) OR
                    (ref->'result_summary'->>'finished_at')::timestamptz IS DISTINCT FROM
                        fact.finished_at
                THEN RAISE EXCEPTION 'invalid terminal dossier reference'; END IF;
            END LOOP;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER validation_dossier_guard BEFORE INSERT OR UPDATE OR DELETE
        ON model_gateway.validation_dossier FOR EACH ROW EXECUTE FUNCTION
            model_gateway.guard_validation_dossier()
    """)
    op.execute(
        "CREATE INDEX ix_model_dossier_history "
        "ON model_gateway.validation_dossier(deployment_id,created_at DESC,id DESC)"
    )
    op.execute("GRANT SELECT, INSERT ON model_gateway.validation_dossier TO model_gateway_rw")
    # PostgreSQL row locking requires UPDATE privilege on one column. The existing
    # check trigger rejects ID changes and every UPDATE without a revision increment;
    # this grant permits locking but cannot mutate a check with the API role.
    op.execute("GRANT UPDATE(id) ON model_gateway.connection_check TO model_gateway_rw")
    op.execute(
        "REVOKE ALL ON FUNCTION model_gateway.valid_dossier_snapshot(JSONB), "
        "model_gateway.guard_validation_dossier() FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION model_gateway.valid_dossier_snapshot(JSONB) TO model_gateway_rw"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM model_gateway.validation_dossier) THEN
            RAISE EXCEPTION 'validation dossier facts prevent downgrade';
        END IF;
    END $$""")
    op.execute("DROP TABLE model_gateway.validation_dossier")
    op.execute("DROP FUNCTION model_gateway.guard_validation_dossier()")
    op.execute("DROP FUNCTION model_gateway.valid_dossier_snapshot(JSONB)")
    op.execute("REVOKE UPDATE(id) ON model_gateway.connection_check FROM model_gateway_rw")
