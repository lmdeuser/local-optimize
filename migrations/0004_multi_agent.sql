-- ============================================================
-- RAG Schema Migration v4
-- Мультиагентная поддержка: agent_id, session_id, A2A очередь
-- PostgreSQL 18 / pgvector / база: aiagent
-- ============================================================

-- 1. Расширяем project_knowledge для мультиагентного доступа
ALTER TABLE project_knowledge ADD COLUMN IF NOT EXISTS agent_id    text;
ALTER TABLE project_knowledge ADD COLUMN IF NOT EXISTS session_id  text;
ALTER TABLE project_knowledge ADD COLUMN IF NOT EXISTS visibility  text DEFAULT 'shared'
    CHECK (visibility IN ('shared', 'private'));
ALTER TABLE project_knowledge ADD COLUMN IF NOT EXISTS importance  smallint DEFAULT 5
    CHECK (importance BETWEEN 1 AND 10);

-- Индексы для фильтрации по агентам
CREATE INDEX IF NOT EXISTS pk_agent_id_idx    ON project_knowledge (agent_id)   WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pk_session_id_idx  ON project_knowledge (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pk_visibility_idx  ON project_knowledge (visibility);

-- 2. Таблица сессий агентов
CREATE TABLE IF NOT EXISTS agent_sessions (
    id           bigserial    PRIMARY KEY,
    agent_id     text         NOT NULL,
    session_id   text         NOT NULL UNIQUE,
    started_at   timestamptz  DEFAULT now(),
    last_seen_at timestamptz  DEFAULT now(),
    model_name   text,
    model_tier   text,
    metadata     jsonb        DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS agent_sessions_agent_idx ON agent_sessions (agent_id);
CREATE INDEX IF NOT EXISTS agent_sessions_seen_idx  ON agent_sessions (last_seen_at DESC);

-- Автообновление last_seen_at через триггер
CREATE OR REPLACE FUNCTION touch_agent_session()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.last_seen_at = now();
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS agent_session_touch ON agent_sessions;
CREATE TRIGGER agent_session_touch
    BEFORE UPDATE ON agent_sessions
    FOR EACH ROW EXECUTE FUNCTION touch_agent_session();

-- 3. A2A очередь сообщений между агентами
CREATE TABLE IF NOT EXISTS agent_messages (
    id           bigserial    PRIMARY KEY,
    from_agent   text         NOT NULL,
    to_agent     text,        -- NULL = broadcast всем
    message_type text         NOT NULL
        CHECK (message_type IN ('knowledge_push', 'query', 'correction', 'invalidate')),
    payload      jsonb        NOT NULL,
    processed    boolean      DEFAULT false,
    created_at   timestamptz  DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_messages_unread_idx
    ON agent_messages (to_agent, processed, created_at)
    WHERE processed = false;

-- 4. Переиндексировать HNSW под 30 GB RAM
-- m=32 даёт лучший recall (было m=16 по умолчанию)
-- ef_construction=256 — более качественный граф при построении
DROP INDEX IF EXISTS project_knowledge_hnsw_idx;
CREATE INDEX project_knowledge_hnsw_idx
    ON project_knowledge
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 32, ef_construction = 256);

-- 5. Статистика (для оптимизатора запросов)
ANALYZE project_knowledge;
ANALYZE kb_entities;
ANALYZE kb_relations;

-- Итог
DO $$
BEGIN
    RAISE NOTICE 'Migration v4 complete: agent_id/session_id columns, agent_sessions, agent_messages, HNSW m=32.';
END;
$$;
