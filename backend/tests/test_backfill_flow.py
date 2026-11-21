"""补传入库、缺测登记、重算排队与失败保留的服务/API 测试。

用内存假仓储替换 PostgreSQL、用捕获器替换 Redis 队列，
因此无需 docker 即可验证完整编排与权限行为。

运行：cd backend && python3 -m pytest tests/test_backfill_flow.py -q
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.middlewares import auth as auth_middleware
from src.services import backfill_service, repository
from src import worker

T0 = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)


class FakeRepository:
    """覆盖 backfill_service / main / worker 会调用的仓储函数。"""

    def __init__(self):
        self.samples = {}          # (case_id, sampled_at) -> row
        self.batches = []
        self.runs = {}
        self.gaps = {1: []}
        self.cases = {1: {'id': 1, 'code': 'MC-T1', 'status': 'active',
                          'rerun_state': 'current', 'last_analysis_run_id': None,
                          'last_rerun_error': None}}
        self.run_sequence = 0
        self.failed_marks = []
        self.running_marks = []
        self.stale_marks = []
        self.events = []

    # ---- 采样
    def find_sample(self, case_id, sampled_at):
        return self.samples.get((case_id, sampled_at))

    def insert_sample(self, case_id, payload, source, user_id, is_late, batch_ref):
        row = {
            'id': len(self.samples) + 1,
            'case_id': case_id,
            'sampled_at': payload['sampled_at'],
            'temperature': payload['temperature'],
            'humidity': payload['humidity'],
            'co2_ppm': payload['co2_ppm'],
            'door_open': payload['door_open'],
            'source': source,
            'state': 'accepted',
            'note': payload.get('note', ''),
            'created_by': user_id,
            'received_at': datetime.now(timezone.utc),
            'is_late': is_late,
            'backfill_batch': batch_ref,
        }
        self.samples[(case_id, payload['sampled_at'])] = row
        return row

    def accepted_samples(self, case_id):
        return sorted(
            [row for row in self.samples.values() if row['case_id'] == case_id],
            key=lambda row: row['sampled_at'],
        )

    def refresh_gaps(self, case_id, samples, batch_ref):
        from src.services.backfill_domain import detect_gaps
        detected = detect_gaps(samples)
        existing = { (g['previous_sample_id'], g['next_sample_id']): g for g in self.gaps[case_id] }
        opened = filled = 0
        for gap in detected:
            pair = (gap.previous_sample_id, gap.next_sample_id)
            if pair not in existing:
                self.gaps[case_id].append({
                    'case_id': case_id, **gap.to_dict(),
                    'detected_batch': batch_ref, 'filled_batch': None,
                })
                opened += 1
        return {'gap_opened': opened, 'gap_filled': filled}

    def update_last_sample_at(self, case_id):
        pass

    # ---- 批次
    def create_backfill_batch(self, case_id, batch_ref, user_id, sample_count, note):
        batch = {'id': len(self.batches) + 1, 'batch_ref': batch_ref, 'case_id': case_id,
                 'submitted_by': user_id, 'sample_count': sample_count, 'note': note}
        self.batches.append(batch)
        return batch['id']

    def finish_backfill_batch(self, batch_id, inserted, duplicates, gap_opened, gap_filled, run_id):
        batch = next(item for item in self.batches if item['id'] == batch_id)
        batch.update(inserted_count=inserted, duplicate_count=duplicates,
                     gap_opened_count=gap_opened, gap_filled_count=gap_filled, run_id=run_id)

    def list_backfill_batches(self, case_id=None):
        rows = [dict(batch) for batch in self.batches]
        if case_id:
            rows = [row for row in rows if row['case_id'] == case_id]
        return rows

    # ---- 展柜状态
    def get_case(self, case_id):
        return self.cases.get(case_id)

    def mark_case_stale(self, case_id):
        self.stale_marks.append(case_id)
        self.cases[case_id]['rerun_state'] = 'stale'

    # ---- 任务
    def create_run(self, case_id, run_key, kind, trigger_source, parent_run_id):
        self.run_sequence += 1
        run = {'id': self.run_sequence, 'run_key': run_key, 'case_id': case_id,
               'status': 'queued', 'kind': kind, 'trigger_source': trigger_source,
               'parent_run_id': parent_run_id}
        self.runs[run['id']] = run
        return dict(run)

    def mark_run_running(self, run_id):
        self.running_marks.append(run_id)
        self.runs[run_id]['status'] = 'running'

    def mark_run_failed_pending(self, run_id, case_id, error):
        self.failed_marks.append((run_id, case_id, error))
        self.runs[run_id]['status'] = 'failed'
        self.runs[run_id]['retry_pending'] = True
        self.cases[case_id]['rerun_state'] = 'failed_pending'
        self.cases[case_id]['last_rerun_error'] = error

    def list_gaps(self, case_id, state=None):
        rows = self.gaps.get(case_id, [])
        if state:
            rows = [row for row in rows if row['state'] == state]
        return rows

    def list_event_versions(self, event_id=None, case_id=None):
        return list(self.events)

    def run_migrations(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv('SEED_DEMO', 'false')
    repo = FakeRepository()
    published = []

    # 替换仓储模块上的全部用到的函数
    for name in (
        'find_sample', 'insert_sample', 'accepted_samples', 'refresh_gaps',
        'update_last_sample_at', 'create_backfill_batch', 'finish_backfill_batch',
        'list_backfill_batches', 'get_case', 'mark_case_stale', 'create_run',
        'mark_run_running', 'mark_run_failed_pending', 'list_gaps',
        'list_event_versions', 'run_migrations',
    ):
        monkeypatch.setattr(repository, name, getattr(repo, name))

    backfill_service.configure_queue(lambda job: published.append(job))

    # 屏蔽启动期真实 DB 等待；main 里的直接 DB 调用与审计
    monkeypatch.setattr('src.main.wait_for_db', lambda *args, **kwargs: None)
    monkeypatch.setattr('src.main.fetch_one', lambda sql, params=(): repo.cases.get(params[0]))
    monkeypatch.setattr('src.main.execute', lambda sql, params=(): 1)
    monkeypatch.setattr('src.main.redis_client.rpush',
                        lambda queue, payload: published.append(payload))

    yield {'repo': repo, 'published': published}


@pytest.fixture
def client(fake):
    operator = {'id': 7, 'username': 'lin-operator', 'display_name': '林芮',
                'role': 'operator', 'active': True}
    app.dependency_overrides[auth_middleware.current_user] = lambda: operator
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _payload(minutes, **overrides):
    item = {'sampled_at': (T0 + timedelta(minutes=minutes)).isoformat(),
            'temperature': 21.0, 'humidity': 49.0, 'co2_ppm': 620,
            'door_open': False, 'note': '网关恢复补传'}
    item.update(overrides)
    return item


# ------------------------------------------------------------- 补传入库

def test_backfill_inserts_by_sample_time_and_queues_rerun(client, fake):
    body = {'samples': [_payload(0), _payload(40), _payload(50)], 'note': '离线网关恢复'}
    response = client.post('/cases/1/backfill', json=body)
    assert response.status_code == 201, response.text
    summary = response.json()
    assert summary['inserted'] == 3
    assert summary['duplicates'] == 0
    assert summary['rerun_queued'] is True
    assert summary['gap_opened'] == 1  # 0 分钟与 40 分钟之间断档 40 分钟

    # 按采样时间入库，且迟到标记为真（采样时间早于当前入库时间）
    rows = fake['repo'].accepted_samples(1)
    assert [row['sampled_at'] for row in rows] == sorted(row['sampled_at'] for row in rows)
    assert all(row['is_late'] for row in rows)
    assert all(row['source'] == 'gateway_backfill' for row in rows)

    # 自动排队的是 backfill_rerun 任务
    assert len(fake['published']) == 1
    job = fake['published'][0]
    assert job['kind'] == 'backfill_rerun'
    assert fake['repo'].cases[1]['rerun_state'] == 'stale'


def test_duplicate_resubmission_creates_no_new_sample_or_run(client, fake):
    body = {'samples': [_payload(0), _payload(10)]}
    first = client.post('/cases/1/backfill', json=body).json()
    assert first['inserted'] == 2
    second = client.post('/cases/1/backfill', json=body).json()
    assert second['inserted'] == 0
    assert second['duplicates'] == 2
    assert second['rerun_queued'] is False
    assert len(fake['repo'].samples) == 2          # 没有重复采样
    assert len(fake['published']) == 1             # 没有第二次排队
    # 原始来源不被覆盖
    assert all(row['source'] == 'gateway_backfill'
               for row in fake['repo'].samples.values())


def test_backfill_rejects_out_of_range_and_empty_batch(client):
    bad_range = {'samples': [_payload(0, humidity=140)]}
    assert client.post('/cases/1/backfill', json=bad_range).status_code == 422
    assert client.post('/cases/1/backfill', json={'samples': []}).status_code == 422
    # 批次内重复时间点
    dup = {'samples': [_payload(0), _payload(0)]}
    assert client.post('/cases/1/backfill', json=dup).status_code == 422


def test_backfill_unknown_case_returns_404(client, monkeypatch):
    monkeypatch.setattr('src.main.fetch_one', lambda sql, params=(): None)
    response = client.post('/cases/999/backfill', json={'samples': [_payload(0)]})
    assert response.status_code == 404


def test_viewer_cannot_backfill(fake):
    viewer = {'id': 9, 'username': 'viewer', 'display_name': '顾言',
              'role': 'viewer', 'active': True}
    app.dependency_overrides[auth_middleware.current_user] = lambda: viewer
    with TestClient(app) as test_client:
        response = test_client.post('/cases/1/backfill', json={'samples': [_payload(0)]})
        assert response.status_code == 403
    app.dependency_overrides.clear()


# ------------------------------------------------------------- 缺测台账

def test_gap_ledger_visible_and_never_synthetic(client, fake):
    client.post('/cases/1/backfill', json={'samples': [_payload(0), _payload(45)]})
    response = client.get('/cases/1/gaps')
    assert response.status_code == 200
    gaps = response.json()
    assert len(gaps) == 1
    assert gaps[0]['gap_seconds'] == 45 * 60
    assert gaps[0]['state'] == 'open'
    # 断档之间没有插入任何采样：仍然只有真实提交的 2 条
    assert len(fake['repo'].accepted_samples(1)) == 2


# ------------------------------------------------------------- 重试与失败保留

def test_retry_only_allowed_for_failed_pending(client, fake):
    # current 状态不允许重试
    response = client.post('/cases/1/rerun-retry')
    assert response.status_code == 409

    fake['repo'].cases[1]['rerun_state'] = 'failed_pending'
    response = client.post('/cases/1/rerun-retry')
    assert response.status_code == 202
    run = response.json()
    assert run['kind'] == 'retry'
    assert len(fake['published']) == 1


def test_worker_failure_keeps_old_results_and_marks_pending(fake, monkeypatch):
    # 预置一条已入库采样，代表旧版结果
    fake['repo'].insert_sample(1, {
        'sampled_at': T0, 'temperature': 21.0, 'humidity': 49.0,
        'co2_ppm': 620, 'door_open': False, 'note': '旧版基线',
    }, source='manual', user_id=7, is_late=False, batch_ref=None)
    fake['repo'].cases[1]['rerun_state'] = 'stale'
    fake['repo'].runs[1] = {'id': 1, 'status': 'queued', 'retry_pending': False}

    def boom(case_id, run_id):
        raise RuntimeError('模型基线不足，重算中断')

    monkeypatch.setattr(worker, 'run_case_analysis', boom)
    worker.process_job({'run_id': 1, 'case_id': 1, 'kind': 'backfill_rerun'})

    # 失败被标记 retry_pending，旧版结果（采样）原样保留
    assert len(fake['repo'].failed_marks) == 1
    run_id, case_id, error = fake['repo'].failed_marks[0]
    assert run_id == 1 and case_id == 1
    assert '模型基线不足' in error
    assert fake['repo'].runs[1]['status'] == 'failed'
    assert fake['repo'].runs[1]['retry_pending'] is True
    assert fake['repo'].cases[1]['rerun_state'] == 'failed_pending'
    assert fake['repo'].samples  # 旧数据未被清空或改写


def test_worker_success_marks_run_running(fake, monkeypatch):
    fake['repo'].runs[1] = {'id': 1, 'status': 'queued', 'retry_pending': False}
    monkeypatch.setattr(worker, 'run_case_analysis', lambda case_id, run_id: ({}, {}))
    worker.process_job({'run_id': 1, 'case_id': 1})
    assert fake['repo'].running_marks == [1]
    assert fake['repo'].failed_marks == []
