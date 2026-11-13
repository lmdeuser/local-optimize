
import pytest

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("numpy")
pytest.importorskip("transformers")

from config import settings
from project_context import ProjectContext, ReadScope, ValidationStatus, WriteVisibility
from rag_engine import RagEngine, build_evidence_acl_sql

# Requires a live PostgreSQL instance: these tests mutate graph tables directly.
pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def clean_db():
    # Never mutate the configured database. Only a database created by this
    # fixture may receive test data or be dropped by its cleanup.
    import secrets
    from psycopg2 import sql
    from migration_runner import MigrationRunner, MigrationError
    from tests.integration.test_fresh_db import _admin_connection, _create_test_database
    admin = _admin_connection()
    database_name = "local_optimize_test_" + secrets.token_hex(8)
    created = False
    conn = None
    try:
        _create_test_database(admin, database_name)
        created = True
        params = {**settings.get_db_params(), "dbname": database_name}
        try:
            MigrationRunner(connection_factory=lambda: psycopg2.connect(**params),
                            expected_database=database_name).apply_all()
        except MigrationError as exc:
            if isinstance(exc.__cause__, psycopg2.errors.InsufficientPrivilege):
                pytest.skip("Isolated database requires vector extension privileges")
            raise
        conn = psycopg2.connect(**params)
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if created:
            with admin.cursor() as cur:
                cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
        admin.close()


@pytest.fixture
def rag(clean_db):
    engine = object.__new__(RagEngine)
    engine._get_conn = lambda: clean_db
    engine._put_conn = lambda *_args, **_kwargs: None
    engine._invalidate_search_cache = lambda *_args, **_kwargs: None
    return engine

def create_context(project_id, root_id, agent_id, session_id, write_visibility, read_scope):
    return ProjectContext(
        project_id=project_id,
        project_name="Test Project",
        root_id=root_id,
        project_root="/tmp",
        allowed_roots=("/tmp",),
        write_visibility=WriteVisibility(write_visibility),
        read_scope=ReadScope(read_scope),
        agent_id=agent_id,
        session_id=session_id,
        validation_status=ValidationStatus.UNVERIFIED,
    )

def test_scenario_a_to_c_visibility(clean_db, rag):
    # Scenario A, B, C: Access Control
    agent1 = "agent_1"
    agent2 = "agent_2"
    session1 = "session_1"
    session2 = "session_2"
    project_id = "proj_test"
    root_id = "primary"

    # Context for writing
    ctx_session_private = create_context(project_id, root_id, agent1, session1, "session-private", "session")
    ctx_agent_private = create_context(project_id, root_id, agent1, session1, "agent-private", "agent")
    ctx_public = create_context(project_id, root_id, agent1, session1, "public", "public")

    with clean_db.cursor() as cur:
        rag._upsert_entity(cur, ctx_session_private, "SessEnt", "key_sess", "concept", "Session private", {}, "ref1")
        rag._upsert_entity(cur, ctx_agent_private, "AgentEnt", "key_agent", "concept", "Agent private", {}, "ref2")
        rag._upsert_entity(cur, ctx_public, "PubEnt", "key_pub", "concept", "Public", {}, "ref3")

    clean_db.commit()

    # The evidence ACL is asserted directly: agentic_search seeds only from vector
    # hits, and this fixture intentionally creates graph rows without any.
    ctx_a = create_context(project_id, root_id, agent1, session1, "session-private", "session")

    def get_visible_entities(ctx):
        sql, params = build_evidence_acl_sql(ctx, "kb_entities", "e")
        with clean_db.cursor() as cur:
            cur.execute(f"SELECT e.name FROM public.kb_entities e WHERE e.project_id = %s AND {sql}", [ctx.project_id] + params)
            return {row[0] for row in cur.fetchall()}

    # Scenario A: Correct agent and session
    visible_a = get_visible_entities(ctx_a)
    assert "SessEnt" in visible_a
    assert "AgentEnt" in visible_a
    assert "PubEnt" in visible_a

    # Scenario B: Same agent, different session, reading with session scope
    ctx_b = create_context(project_id, root_id, agent1, session2, "session-private", "session")
    visible_b = get_visible_entities(ctx_b)
    assert "SessEnt" not in visible_b
    assert "AgentEnt" in visible_b
    assert "PubEnt" in visible_b

    # Scenario C: Different agent
    ctx_c = create_context(project_id, root_id, agent2, session1, "session-private", "session")
    visible_c = get_visible_entities(ctx_c)
    assert "SessEnt" not in visible_c
    assert "AgentEnt" not in visible_c
    assert "PubEnt" in visible_c

def test_scenario_d_public(clean_db, rag):
    # Scenario D: Public is available to everyone
    ctx_pub = create_context("proj_test", "primary", "ag1", "sess1", "public", "public")
    with clean_db.cursor() as cur:
        rag._upsert_entity(cur, ctx_pub, "PubEnt2", "key_pub2", "concept", "Public", {}, "ref_pub")
    clean_db.commit()
    
    ctx_other = create_context("proj_test", "primary", "ag2", "sess2", "session-private", "session")
    sql, params = build_evidence_acl_sql(ctx_other, "kb_entities", "e")
    with clean_db.cursor() as cur:
        cur.execute(
            f"SELECT e.name FROM public.kb_entities e WHERE e.project_id = %s AND {sql}",
            [ctx_other.project_id, *params],
        )
        res = {row[0] for row in cur.fetchall()}
    assert "PubEnt2" in res

def test_scenario_e_orphan_cleanup(clean_db, rag):
    # Scenario E: Orphan Cleanup via delete_document
    ctx = create_context("proj_test", "primary", "ag1", "sess1", "public", "public")
    with clean_db.cursor() as cur:
        e1 = rag._upsert_entity(cur, ctx, "Ent1", "key1", "concept", "Test 1", {}, "test_file.py")
        e2 = rag._upsert_entity(cur, ctx, "Ent2", "key2", "concept", "Test 2", {}, "test_file.py")
        rag._upsert_relation(cur, ctx, e1, e2, "relates", source_ref="test_file.py")
    clean_db.commit()
    
    # Run delete_document
    rag.delete_document(ctx, "/tmp/test_file.py")
    
    # Check if entities and relations are deleted
    with clean_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.kb_entities WHERE project_id = 'proj_test'")
        ent_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.kb_relations WHERE project_id = 'proj_test'")
        rel_count = cur.fetchone()[0]
        
    assert ent_count == 0, "Orphan entities were not cleaned up"
    assert rel_count == 0, "Orphan relations were not cleaned up"

def test_scenario_f_root_id_isolation(clean_db, rag):
    # Scenario F: Root isolation
    ctx_root1 = create_context("proj_test", "root1", "ag1", "sess1", "public", "public")
    ctx_root2 = create_context("proj_test", "root2", "ag1", "sess1", "public", "public")
    
    with clean_db.cursor() as cur:
        rag._upsert_entity(cur, ctx_root1, "EntR1", "keyR1", "concept", "Root 1", {}, "ref1")
        rag._upsert_entity(cur, ctx_root2, "EntR2", "keyR2", "concept", "Root 2", {}, "ref2")
    clean_db.commit()
    
    def get_visible(ctx):
        sql, params = build_evidence_acl_sql(ctx, "kb_entities", "e")
        with clean_db.cursor() as cur:
            cur.execute(
                f"SELECT e.name FROM public.kb_entities e WHERE e.project_id = %s AND {sql}",
                [ctx.project_id, *params],
            )
            return {row[0] for row in cur.fetchall()}

    # Documents current behavior: the evidence ACL is project- and owner-scoped and
    # deliberately carries no root_id predicate, so both roots stay visible here.
    # Root confinement is enforced by ProjectContext path resolution, not by this SQL.
    v1 = get_visible(ctx_root1)
    assert "EntR1" in v1
    assert "EntR2" in v1


def test_entity_collision_cannot_overwrite_another_private_owner(clean_db, rag):
    owner = create_context('proj_test', 'primary', 'owner', 'session', 'session-private', 'session')
    other = create_context('proj_test', 'primary', 'other', 'session', 'session-private', 'session')
    with clean_db.cursor() as cur:
        entity_id = rag._upsert_entity(cur, owner, 'Private', 'collision-key', 'concept', 'original', {}, 'source')
    clean_db.commit()
    try:
        with clean_db.cursor() as cur:
            with pytest.raises(PermissionError):
                rag._upsert_entity(cur, other, 'Private', 'collision-key', 'concept', 'overwritten', {}, 'source')
    finally:
        clean_db.rollback()
    with clean_db.cursor() as cur:
        cur.execute('SELECT description, agent_id FROM public.kb_entities WHERE id = %s', (entity_id,))
        assert cur.fetchone() == ('original', 'owner')
        cur.execute('SELECT count(*) FROM public.entity_evidence WHERE entity_id = %s AND agent_id = %s', (entity_id, 'other'))
        assert cur.fetchone()[0] == 0


def test_delete_does_not_remove_another_private_owner(clean_db, rag):
    owner = create_context('proj_test', 'primary', 'owner', 'session', 'session-private', 'session')
    other = create_context('proj_test', 'primary', 'other', 'session', 'session-private', 'session')
    with clean_db.cursor() as cur:
        entity_id = rag._upsert_entity(cur, owner, 'Private', 'private-file', 'concept', 'original', {}, 'private.py')
    clean_db.commit()
    assert rag.delete_document(other, 'private.py')['status'] == 'success'
    with clean_db.cursor() as cur:
        cur.execute('SELECT count(*) FROM public.entity_evidence WHERE entity_id = %s', (entity_id,))
        assert cur.fetchone()[0] == 1
        cur.execute('SELECT count(*) FROM public.kb_entities WHERE id = %s', (entity_id,))
        assert cur.fetchone()[0] == 1


def test_feedback_does_not_weaken_another_owners_private_relation(clean_db, rag):
    public = create_context('proj_test', 'primary', 'owner', 'session', 'public', 'public')
    owner = create_context('proj_test', 'primary', 'owner', 'session', 'session-private', 'session')
    other = create_context('proj_test', 'primary', 'other', 'session', 'session-private', 'session')
    with clean_db.cursor() as cur:
        ids = [rag._upsert_entity(cur, public, name, name, 'concept', name, {}, 'source')
               for name in ['QueryNode', 'CorrectionNode', 'PreviousNode']]
        relation = rag._upsert_relation(cur, owner, ids[0], ids[2], 'refines', weight=1.0, source_ref='private')
    clean_db.commit()
    assert rag.log_feedback(other, 'correction', 'QueryNode', 'CorrectionNode')['status'] == 'success'
    with clean_db.cursor() as cur:
        cur.execute('SELECT weight FROM public.kb_relations WHERE id = %s', (relation,))
        assert float(cur.fetchone()[0]) == 1.0
        independent = rag._upsert_relation(cur, other, ids[0], ids[2], 'refines', source_ref='foreign')
        assert independent is not None and independent != relation
        cur.execute('SELECT count(*) FROM public.relation_evidence WHERE relation_id = %s AND agent_id = %s', (relation, 'other'))
        assert cur.fetchone()[0] == 0


def test_rootless_writes_and_project_enumeration_enforce_session_acl(clean_db, rag):
    import numpy as np
    from project_context import GlobalKnowledgeContext, GlobalWriteContext

    rag.encode = lambda *_args, **_kwargs: np.zeros(384, dtype=np.float32)
    owner = GlobalWriteContext("session-private", "session", "owner", "session-a")
    other = GlobalWriteContext("session-private", "session", "owner", "session-b")
    result = rag.save(owner, "Private fixture content that remains session scoped")
    assert result["status"] == "success"
    read_owner = GlobalKnowledgeContext("session", "owner", "session-a")
    read_other = GlobalKnowledgeContext("session", "owner", "session-b")
    assert any(project == owner.project_id for project, _ in rag.list_readable_projects(read_owner))
    assert not rag.list_readable_projects(read_other)
    assert rag.deprecate(other, chunk_id=result["chunk_id"])["updated"] == 0
    assert rag.verify(other, chunk_id=result["chunk_id"])["updated"] == 0
    assert rag.verify(owner, chunk_id=result["chunk_id"])["updated"] == 1
    with pytest.raises(ValueError, match="filesystem"):
        rag.save(owner, "Attempt to attach a file to rootless knowledge", file_path="a.py")


def test_ad_hoc_indexing_persists_source_and_checks_freshness(clean_db, rag, tmp_path, monkeypatch):
    import numpy as np
    import rag_engine
    from project_context import GlobalKnowledgeContext, ProjectRegistry

    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "note.md"
    source.write_text("A workspace document with enough content to be indexed. " * 3)
    registry = ProjectRegistry([])
    monkeypatch.setattr(rag_engine, "get_project_registry", lambda _: registry)
    context = registry.resolve(working_directory=str(root), write_visibility="session-private",
                               read_scope="session", agent_id="agent", session_id="session")
    rag.encode = lambda *_args, **_kwargs: np.zeros(384, dtype=np.float32)
    rag._chunk_text = lambda text, **_kwargs: [text]
    assert rag.index_document(context, str(source))["status"] == "success"
    with clean_db.cursor() as cur:
        cur.execute("SELECT id, project_id, root_id, file_path, source_uri, metadata FROM public.project_knowledge")
        row = cur.fetchone()
    hit = dict(zip(("chunk_id", "project_id", "root_id", "file_path", "source_uri", "metadata"), row))
    hit["final_score"] = 0.8
    checked = rag._check_source_versions(context.project_id, {"status": "success", "results": [hit]})
    assert checked["results"][0]["source_state"] == "current"
    assert rag.list_readable_projects(GlobalKnowledgeContext("session", "agent", "session"))[0][0] == context.project_id
    assert not rag.list_readable_projects(GlobalKnowledgeContext("session", "agent", "other"))


def test_unregistered_legacy_source_uses_its_stored_root(clean_db, rag, tmp_path, monkeypatch):
    import numpy as np
    import rag_engine
    from project_context import ProjectDefinition, ProjectRegistry

    root = tmp_path / "legacy"
    root.mkdir()
    source = root / "note.md"
    source.write_text("A legacy document with enough content to be indexed. " * 3)
    registry = ProjectRegistry([ProjectDefinition("legacy", "Legacy", {"primary": root})])
    monkeypatch.setattr(rag_engine, "get_project_registry", lambda _: registry)
    context = registry.resolve(project_id="legacy", root_id="primary", write_visibility="shared",
                               read_scope="shared", agent_id="agent", session_id="session")
    rag.encode = lambda *_args, **_kwargs: np.zeros(384, dtype=np.float32)
    rag._chunk_text = lambda text, **_kwargs: [text]
    assert rag.index_document(context, str(source))["status"] == "success"
    with clean_db.cursor() as cur:
        cur.execute("SELECT id, project_id, root_id, file_path, source_uri, metadata FROM public.project_knowledge")
        row = cur.fetchone()
    hit = dict(zip(("chunk_id", "project_id", "root_id", "file_path", "source_uri", "metadata"), row))
    hit["final_score"] = 0.8
    registry = ProjectRegistry([])
    checked = rag._check_source_versions_with_provenance("legacy", {"status": "success", "results": [hit]})
    assert checked["results"][0]["source_state"] == "current"
