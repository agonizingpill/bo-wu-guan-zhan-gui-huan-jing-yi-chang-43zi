"""PostgreSQL 仓储：迁移、补传入库、缺测台账、事件重算与版本血缘。

所有重算写操作放在单个事务里：任何一步失败整体回滚，旧版事件保持不动，
仅把展柜标记为 failed_pending、算法任务标记 retry_pending。
"""

from __future__ import annotations

import json
from pathlib import Path

from .backfill_domain import (
    EVENT_FIELDS,
    detect_gaps,
    plan_rerun,
    snapshot,
)
from ..utils.db import get_conn

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / 'migrations'


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def run_migrations() -> None:
    """按文件名顺序执行 migrations 目录下的 SQL，失败即抛错阻止启动。"""
    for path in sorted(MIGRATIONS_DIR.glob('*.sql')):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding='utf-8'))


# ---------------------------------------------------------------- 采样与缺测

def find_sample(case_id: int, sampled_at) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT * FROM environment_samples WHERE case_id=%s AND sampled_at=%s',
                (case_id, sampled_at),
            )
            row = cur.fetchone()
            if not row:
                return None
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row, strict=True))


def insert_sample(case_id: int, payload: dict, source: str, user_id: int | None,
                  is_late: bool, batch_ref: str | None) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''INSERT INTO environment_samples
                   (case_id, sampled_at, temperature, humidity, co2_ppm, door_open, source, state,
                    note, created_by, received_at, is_late, backfill_batch)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'accepted',%s,%s,NOW(),%s,%s)
                   ON CONFLICT (case_id, sampled_at) DO NOTHING RETURNING *''',
                (case_id, payload['sampled_at'], payload['temperature'], payload['humidity'],
                 payload['co2_ppm'], payload['door_open'], source, payload.get('note', ''),
                 user_id, is_late, batch_ref),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    'SELECT * FROM environment_samples WHERE case_id=%s AND sampled_at=%s',
                    (case_id, payload['sampled_at']),
                )
                existing = cur.fetchone()
                columns = [desc[0] for desc in cur.description]
                return dict(zip(columns, existing, strict=True))
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row, strict=True))


def accepted_samples(case_id: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''SELECT s.*, c.target_temp_min, c.target_temp_max, c.target_rh_min, c.target_rh_max
                   FROM environment_samples s JOIN display_cases c ON c.id=s.case_id
                   WHERE s.case_id=%s AND s.state='accepted' ORDER BY s.sampled_at''',
                (case_id,),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def list_gaps(case_id: int, state: str | None = None) -> list[dict]:
    sql = 'SELECT * FROM measurement_gaps WHERE case_id=%s'
    params: list = [case_id]
    if state:
        sql += ' AND state=%s'
        params.append(state)
    sql += ' ORDER BY previous_at'
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def refresh_gaps(case_id: int, samples: list[dict], batch_ref: str | None) -> dict:
    """对照当前时序重算缺测台账，返回新增/补齐数量。幂等。"""
    detected = detect_gaps(samples)
    opened = filled = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            existing = {
                (row[1], row[2]): row
                for row in _query_tuples(
                    cur,
                    'SELECT id, previous_sample_id, next_sample_id, state FROM measurement_gaps WHERE case_id=%s',
                    (case_id,),
                )
            }
            current_pairs = {(gap.previous_sample_id, gap.next_sample_id): gap for gap in detected}

            for pair, gap in current_pairs.items():
                if pair not in existing:
                    cur.execute(
                        '''INSERT INTO measurement_gaps
                           (case_id, previous_sample_id, next_sample_id, previous_at, next_at,
                            gap_seconds, state, detected_batch)
                           VALUES (%s,%s,%s,%s,%s,%s,'open',%s)''',
                        (case_id, gap.previous_sample_id, gap.next_sample_id, gap.previous_at,
                         gap.next_at, gap.gap_seconds, batch_ref),
                    )
                    opened += 1

            for pair, row in existing.items():
                gap_id, _, _, old_state = row
                if pair not in current_pairs and old_state == 'open':
                    cur.execute(
                        '''UPDATE measurement_gaps SET state='filled', filled_batch=%s, filled_at=NOW()
                           WHERE id=%s''',
                        (batch_ref, gap_id),
                    )
                    filled += 1
    return {'gap_opened': opened, 'gap_filled': filled}


def _query_tuples(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchall()


def create_backfill_batch(case_id: int, batch_ref: str, user_id: int | None,
                          sample_count: int, note: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''INSERT INTO backfill_batches (batch_ref, case_id, submitted_by, sample_count, note)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id''',
                (batch_ref, case_id, user_id, sample_count, note),
            )
            return cur.fetchone()[0]


def finish_backfill_batch(batch_id: int, inserted: int, duplicates: int,
                          gap_opened: int, gap_filled: int, run_id: int | None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''UPDATE backfill_batches SET inserted_count=%s, duplicate_count=%s,
                   gap_opened_count=%s, gap_filled_count=%s, run_id=%s WHERE id=%s''',
                (inserted, duplicates, gap_opened, gap_filled, run_id, batch_id),
            )


def list_backfill_batches(case_id: int | None = None) -> list[dict]:
    sql = '''SELECT b.*, u.display_name AS submitted_by_name
             FROM backfill_batches b LEFT JOIN users u ON u.id=b.submitted_by'''
    params: list = []
    if case_id:
        sql += ' WHERE b.case_id=%s'
        params.append(case_id)
    sql += ' ORDER BY b.created_at DESC LIMIT 100'
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def mark_case_stale(case_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE display_cases SET rerun_state='stale' WHERE id=%s AND rerun_state='current'",
                (case_id,),
            )


def update_last_sample_at(case_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''UPDATE display_cases SET last_sample_at=(
                       SELECT MAX(sampled_at) FROM environment_samples WHERE case_id=%s
                   ) WHERE id=%s''',
                (case_id, case_id),
            )


# ------------------------------------------------------------- 算法任务与重算

def get_case(case_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM display_cases WHERE id=%s', (case_id,))
            row = cur.fetchone()
            if not row:
                return None
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row, strict=True))


def create_run(case_id: int, run_key: str, kind: str, trigger_source: str | None,
               parent_run_id: int | None) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''INSERT INTO algorithm_runs
                   (run_key, case_id, status, kind, trigger_source, parent_run_id, input_summary, parameters)
                   VALUES (%s,%s,'queued',%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING *''',
                (run_key, case_id, kind, trigger_source, parent_run_id,
                 _json({'case_id': case_id}),
                 _json({'random_state': 1069, 'contamination': 0.22, 'scaler': 'RobustScaler'})),
            )
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, cur.fetchone(), strict=True))


def mark_run_running(run_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE algorithm_runs SET status='running', retry_pending=FALSE, updated_at=NOW() WHERE id=%s",
                (run_id,),
            )


def mark_run_failed_pending(run_id: int, case_id: int, error: str) -> None:
    """重算失败：保留旧版结果，任务与展柜都进入待处理。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''UPDATE algorithm_runs SET status='failed', error=%s, retry_pending=TRUE,
                   updated_at=NOW() WHERE id=%s''',
                (error, run_id),
            )
            cur.execute(
                '''UPDATE display_cases SET rerun_state='failed_pending', last_rerun_error=%s
                   WHERE id=%s''',
                (error, case_id),
            )


def apply_rerun(case_id: int, run_id: int, rows: list[dict], candidates: list[dict],
                open_gap_ids: set[int]) -> dict:
    """单事务应用重算结果。返回变更统计与血缘明细。

    - status='closed' 的事件冻结，永不修改，其判定与巡检证据保持原样；
    - 其余当前事件按候选对账：更新 / 撤销（superseded 留痕）/ 新建；
    - 所有变化写入 anomaly_event_versions，记录重算前后快照。
    决策由 backfill_domain.plan_rerun 生成，本函数只负责事务化落库。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 行锁阻止同展柜并发重算
            cur.execute('SELECT id FROM display_cases WHERE id=%s FOR UPDATE', (case_id,))
            cur.execute(
                '''SELECT * FROM anomaly_events WHERE case_id=%s AND revision_status='current'
                   ORDER BY sample_id FOR UPDATE''',
                (case_id,),
            )
            columns = [desc[0] for desc in cur.description]
            current = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

            plan = plan_rerun(current, candidates)
            lineage: list[dict] = []

            for operation in plan['operations']:
                before = operation['before']
                after = operation['after']
                sample_id = operation['sample_id']
                change_type = operation['change_type']
                group_change = operation['group_change']

                if operation['op'] == 'create':
                    cur.execute(
                        '''INSERT INTO anomaly_events
                           (case_id, sample_id, algorithm_run_id, score, threshold, anomaly_type, severity,
                            status, door_context, consecutive_count, escalated_to_curator, reason,
                            revision_status, gap_affected, created_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'current',%s,NULL)
                           RETURNING id''',
                        (case_id, sample_id, run_id, after['score'], after['threshold'],
                         after['anomaly_type'], after['severity'], after['status'],
                         after['door_context'], after['consecutive_count'],
                         after['escalated_to_curator'], after['reason'], after['gap_affected']),
                    )
                    new_id = cur.fetchone()[0]
                    lineage.append(_version_row(cur, new_id, case_id, sample_id, run_id,
                                                 change_type, group_change, {}, after,
                                                 operation['note']))

                elif operation['op'] == 'revoke':
                    cur.execute(
                        '''UPDATE anomaly_events SET revision_status='superseded',
                           superseded_at=NOW(), superseded_by_run=%s, revision_note=%s
                           WHERE id=%s RETURNING id''',
                        (run_id, operation['note'], before['id']),
                    )
                    lineage.append(_version_row(cur, before['id'], case_id, sample_id, run_id,
                                                 'revoked', 'same', before, None,
                                                 operation['note']))

                elif operation['op'] == 'update':
                    cur.execute(
                        '''UPDATE anomaly_events SET algorithm_run_id=%s, score=%s, threshold=%s,
                           anomaly_type=%s, severity=%s, status=%s, door_context=%s,
                           consecutive_count=%s, escalated_to_curator=%s, reason=%s,
                           gap_affected=%s WHERE id=%s RETURNING id''',
                        (run_id, after['score'], after['threshold'], after['anomaly_type'],
                         after['severity'], after['status'], after['door_context'],
                         after['consecutive_count'], after['escalated_to_curator'], after['reason'],
                         after['gap_affected'], before['id']),
                    )
                    lineage.append(_version_row(cur, before['id'], case_id, sample_id, run_id,
                                                 change_type, group_change, before, after,
                                                 operation['note']))
                else:
                    lineage.append(_version_row(cur, before['id'], case_id, sample_id, run_id,
                                                 'unchanged', 'same', before, after,
                                                 operation['note']))

            cur.execute(
                '''UPDATE display_cases SET rerun_state='current', last_rerun_error=NULL,
                   last_analysis_run_id=%s WHERE id=%s''',
                (run_id, case_id),
            )
    return {'stats': plan['stats'], 'lineage': lineage,
            'frozen_sample_ids': plan['frozen_sample_ids']}


def _version_row(cur, event_id, case_id, sample_id, run_id, change_type, group_change,
                 before, after, note) -> dict:
    cur.execute(
        '''INSERT INTO anomaly_event_versions
           (event_id, case_id, sample_id, algorithm_run_id, change_type, group_change,
            before_snapshot, after_snapshot, change_note)
           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
           RETURNING id, change_type, group_change, change_note''',
        (event_id, case_id, sample_id, run_id, change_type, group_change,
         _json(snapshot(before) if before else {}),
         _json(snapshot(after) if after else {}), note),
    )
    row = cur.fetchone()
    return {'version_id': row[0], 'event_id': event_id, 'sample_id': sample_id,
            'change_type': row[1], 'group_change': row[2], 'change_note': row[3]}


def finish_run_success(run_id: int, metrics: dict, result: dict) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''UPDATE algorithm_runs SET status='succeeded', metrics=%s::jsonb,
                   result=%s::jsonb, retry_pending=FALSE, updated_at=NOW() WHERE id=%s''',
                (_json(metrics), _json(result), run_id),
            )


def list_event_versions(event_id: int | None = None, case_id: int | None = None) -> list[dict]:
    sql = '''SELECT v.*, s.sampled_at FROM anomaly_event_versions v
             JOIN environment_samples s ON s.id=v.sample_id'''
    clauses = []
    params: list = []
    if event_id:
        clauses.append('v.event_id=%s')
        params.append(event_id)
    if case_id:
        clauses.append('v.case_id=%s')
        params.append(case_id)
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY v.created_at, v.id'
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
