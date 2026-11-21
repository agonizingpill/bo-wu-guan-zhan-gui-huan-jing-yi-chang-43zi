-- 002: 离线补传、缺测台账与异常重算追溯
-- 幂等：可在全新库与已存在 001 结构的库上重复执行。

-- 1. 采样增加入库时间、迟到标记与补传批次
ALTER TABLE environment_samples
    ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS is_late BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS backfill_batch VARCHAR(60);

-- 2. 异常事件增加版本状态：当前 / 已被重算取代；关闭事件冻结保留
ALTER TABLE anomaly_events
    ADD COLUMN IF NOT EXISTS revision_status VARCHAR(20) NOT NULL DEFAULT 'current',
    ADD COLUMN IF NOT EXISTS revision_note TEXT,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_by_run INTEGER,
    ADD COLUMN IF NOT EXISTS gap_affected BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'anomaly_events_revision_status_chk') THEN
        ALTER TABLE anomaly_events
            ADD CONSTRAINT anomaly_events_revision_status_chk CHECK (revision_status IN ('current','superseded'));
    END IF;
END $$;

-- 同一采样点最多保留一条当前版本事件（历史版本以 superseded 留痕）
CREATE UNIQUE INDEX IF NOT EXISTS uq_anomaly_current_sample
    ON anomaly_events(sample_id) WHERE revision_status = 'current';

-- 3. 算法任务增加任务类型、触发来源、待重试标记与父任务
ALTER TABLE algorithm_runs
    ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'analysis',
    ADD COLUMN IF NOT EXISTS trigger_source VARCHAR(60),
    ADD COLUMN IF NOT EXISTS retry_pending BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS parent_run_id INTEGER REFERENCES algorithm_runs(id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'algorithm_runs_kind_chk') THEN
        ALTER TABLE algorithm_runs
            ADD CONSTRAINT algorithm_runs_kind_chk CHECK (kind IN ('analysis','backfill_rerun','retry'));
    END IF;
END $$;

-- 4. 展柜增加重算状态：current 正常 / stale 数据已变待重算 / failed_pending 重算失败待处理
ALTER TABLE display_cases
    ADD COLUMN IF NOT EXISTS rerun_state VARCHAR(20) NOT NULL DEFAULT 'current',
    ADD COLUMN IF NOT EXISTS last_rerun_error TEXT,
    ADD COLUMN IF NOT EXISTS last_analysis_run_id INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'display_cases_rerun_state_chk') THEN
        ALTER TABLE display_cases
            ADD CONSTRAINT display_cases_rerun_state_chk CHECK (rerun_state IN ('current','stale','failed_pending'));
    END IF;
END $$;

-- 5. 缺测时段台账：显式记录超过 30 分钟的断档，补齐后标记 filled，绝不生成虚假正常采样
CREATE TABLE IF NOT EXISTS measurement_gaps (
    id SERIAL PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
    previous_sample_id INTEGER NOT NULL REFERENCES environment_samples(id) ON DELETE CASCADE,
    next_sample_id INTEGER NOT NULL REFERENCES environment_samples(id) ON DELETE CASCADE,
    previous_at TIMESTAMPTZ NOT NULL,
    next_at TIMESTAMPTZ NOT NULL,
    gap_seconds INTEGER NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'open' CHECK (state IN ('open','filled')),
    detected_batch VARCHAR(60),
    filled_batch VARCHAR(60),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filled_at TIMESTAMPTZ,
    UNIQUE(case_id, previous_sample_id, next_sample_id)
);

CREATE INDEX IF NOT EXISTS idx_measurement_gaps_case ON measurement_gaps(case_id, state);

-- 6. 异常事件版本血缘：每次重算的合并、拆分、升级变化都留下重算前后快照
CREATE TABLE IF NOT EXISTS anomaly_event_versions (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES anomaly_events(id) ON DELETE CASCADE,
    case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL REFERENCES environment_samples(id) ON DELETE CASCADE,
    algorithm_run_id INTEGER REFERENCES algorithm_runs(id),
    change_type VARCHAR(30) NOT NULL CHECK (change_type IN (
        'fresh','recalculated','revoked','merged','split',
        'escalation_added','escalation_removed','type_change','unchanged'
    )),
    group_change VARCHAR(20) NOT NULL DEFAULT 'same' CHECK (group_change IN ('same','new','merge','split')),
    before_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    change_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_versions_event ON anomaly_event_versions(event_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_versions_case ON anomaly_event_versions(case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_versions_run ON anomaly_event_versions(algorithm_run_id);

-- 7. 补传批次台账
CREATE TABLE IF NOT EXISTS backfill_batches (
    id SERIAL PRIMARY KEY,
    batch_ref VARCHAR(60) UNIQUE NOT NULL,
    case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
    submitted_by INTEGER REFERENCES users(id),
    sample_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    gap_opened_count INTEGER NOT NULL DEFAULT 0,
    gap_filled_count INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backfill_batches_case ON backfill_batches(case_id, created_at);
