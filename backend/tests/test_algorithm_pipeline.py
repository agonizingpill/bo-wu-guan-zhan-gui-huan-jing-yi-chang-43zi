"""真实 sklearn 模型 + 假仓储的算法编排冒烟测试。

只验证模型→候选→重算落库的管线完整性；升级/合并/拆分的判定口径
由 test_backfill_domain.py 与 test_rerun_scenario.py 精确覆盖。
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.services import algorithm, repository

T0 = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake_repo(monkeypatch):
    class FakeRepo:
        def __init__(self):
            self.samples = []
            self.gaps = []
            self.applied = []
            self.finished = []

        def accepted_samples(self, case_id):
            return list(self.samples)

        def list_gaps(self, case_id):
            return list(self.gaps)

        def apply_rerun(self, case_id, run_id, rows, candidates, open_gap_ids):
            self.applied.append({'candidates': candidates, 'rows': rows})
            return {'stats': {'created': len(candidates), 'updated': 0, 'revoked': 0,
                              'unchanged': 0, 'frozen': 0},
                    'lineage': [], 'frozen_sample_ids': []}

        def finish_run_success(self, run_id, metrics, result):
            self.finished.append({'metrics': metrics, 'result': result})

    fake = FakeRepo()
    for name in ('accepted_samples', 'list_gaps', 'apply_rerun', 'finish_run_success'):
        monkeypatch.setattr(repository, name, getattr(fake, name))
    return fake


def _sample(sample_id, minutes, temp, rh, co2, door=False):
    return {'id': sample_id, 'case_id': 1,
            'sampled_at': T0 + timedelta(minutes=minutes),
            'temperature': temp, 'humidity': rh, 'co2_ppm': co2,
            'door_open': door, 'state': 'accepted',
            'target_temp_min': 18, 'target_temp_max': 24,
            'target_rh_min': 40, 'target_rh_max': 55}


def _mixed_samples():
    # 10 条稳态 + 5 条明显偏离的采样，满足 >=6 条基线并制造离群空间
    stable = [_sample(i, i * 10, 21.0 + i * 0.05, 49.0 + i * 0.03, 620 + i * 3)
              for i in range(1, 11)]
    outliers = [_sample(i, i * 10, 27.5 + i * 0.2, 78.0 + i * 0.4, 1800 + i * 15)
                for i in range(11, 16)]
    return stable + outliers


def test_algorithm_pipeline_runs_real_sklearn_and_persists(fake_repo):
    fake_repo.samples = _mixed_samples()
    metrics, result = algorithm.run_case_analysis(1, 77)

    assert metrics['sample_count'] == 15
    assert set(metrics) >= {'outlier_count', 'threshold', 'rebuild_stats', 'open_gap_count'}
    assert result['detector'].startswith('IsolationForest')
    # 候选与输入行数一致传给重算层，且字段完整
    rows = fake_repo.applied[0]['rows']
    assert len(rows) == 15
    assert all({'id', 'abnormal', 'score', 'threshold', 'door_open'} <= set(row) for row in rows)
    assert fake_repo.finished[0]['metrics']['sample_count'] == 15


def test_algorithm_requires_six_samples(fake_repo):
    fake_repo.samples = [_sample(i, i * 10, 21.0, 49.0, 620) for i in range(1, 5)]
    with pytest.raises(ValueError, match='至少需要 6 条'):
        algorithm.run_case_analysis(1, 78)
    # 基线不足时没有任何重算落库
    assert fake_repo.applied == []


def test_algorithm_records_open_gaps_and_flags_boundary_samples(fake_repo):
    fake_repo.samples = _mixed_samples()
    fake_repo.gaps = [{
        'case_id': 1, 'previous_sample_id': 3, 'next_sample_id': 4,
        'state': 'open', 'gap_seconds': 3600,
    }]
    metrics, _ = algorithm.run_case_analysis(1, 79)
    assert metrics['open_gap_count'] == 1
    rows = {row['id']: row for row in fake_repo.applied[0]['rows']}
    assert rows[3]['gap_affected'] is True
    assert rows[4]['gap_affected'] is True
    assert rows[5]['gap_affected'] is False


def test_algorithm_metrics_are_kept_when_repository_commits(fake_repo):
    fake_repo.samples = _mixed_samples()
    metrics, result = algorithm.run_case_analysis(1, 80)
    assert result['feature_order'] == ['temperature_c', 'relative_humidity_pct', 'co2_ppm']
    assert result['frozen_closed_event_samples'] == []
    assert isinstance(metrics['outlier_rate'], float)
