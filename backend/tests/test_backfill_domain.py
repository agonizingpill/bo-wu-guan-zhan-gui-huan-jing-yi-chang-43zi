"""离线补传与重算追溯的纯领域逻辑测试（无数据库、无 sklearn）。

运行：cd backend && python3 -m pytest tests/test_backfill_domain.py -q
"""

from datetime import datetime, timedelta, timezone

from src.services import backfill_domain as domain

T0 = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)


def _sample(sample_id, minutes, abnormal=False, door=False, score=-0.2, threshold=-0.1):
    return {
        'id': sample_id,
        'sampled_at': T0 + timedelta(minutes=minutes),
        'door_open': door,
        'abnormal': abnormal,
        'score': score,
        'threshold': threshold,
    }


def _event(sample_id, status='open', anomaly_type='environment_outlier',
           severity='high', door_context=False, consecutive_count=1,
           escalated=False, score=-0.2, threshold=-0.1, reason='x'):
    return {
        'id': 1000 + sample_id,
        'sample_id': sample_id,
        'status': status,
        'anomaly_type': anomaly_type,
        'severity': severity,
        'door_context': door_context,
        'consecutive_count': consecutive_count,
        'escalated_to_curator': escalated,
        'reason': reason,
        'score': score,
        'threshold': threshold,
    }


# --------------------------------------------------------------- 缺测与迟到

def test_detect_gaps_flags_only_intervals_over_30_minutes():
    samples = [_sample(1, 0), _sample(2, 10), _sample(3, 50), _sample(4, 60)]
    gaps = domain.detect_gaps(samples)
    assert len(gaps) == 1
    assert gaps[0].previous_sample_id == 2
    assert gaps[0].next_sample_id == 3
    assert gaps[0].gap_seconds == 40 * 60
    assert gaps[0].state == 'open'


def test_detect_gaps_unsorted_input_still_chronological():
    samples = [_sample(3, 50), _sample(1, 0), _sample(2, 10)]
    gaps = domain.detect_gaps(samples)
    assert [(g.previous_sample_id, g.next_sample_id) for g in gaps] == [(2, 3)]


def test_no_synthetic_normal_samples_are_created_for_gaps():
    # 缺测识别只返回 Gap 记录，调用方不会拿到任何可入库的“正常采样”
    samples = [_sample(1, 0), _sample(2, 90)]
    gaps = domain.detect_gaps(samples)
    assert len(gaps) == 1
    assert all(isinstance(gap, domain.Gap) for gap in gaps)


def test_late_sample_detection():
    received = T0 + timedelta(hours=3)
    assert domain.is_late_sample(T0, received) is True
    assert domain.is_late_sample(received, T0) is False
    assert domain.is_late_sample(None, received) is False


# ------------------------------------------------- 连续计数不跨缺测断档

def test_streak_resets_across_open_gap_and_blocks_escalation():
    rows = [
        _sample(1, 0, abnormal=True),
        _sample(2, 10, abnormal=True),
        # 3 号与 2 号之间存在开放缺测
        _sample(3, 60, abnormal=True),
        _sample(4, 70, abnormal=True),
    ]
    gap_pairs = {(2, 3)}
    candidates = domain.build_candidates(rows, gap_pairs)
    by_sample = {item['sample_id']: item for item in candidates}
    # 断档后 streak 重新计数，任何一点都到不了 3，不应升级馆长
    assert by_sample[1]['consecutive_count'] == 1
    assert by_sample[2]['consecutive_count'] == 2
    assert by_sample[3]['consecutive_count'] == 1
    assert by_sample[4]['consecutive_count'] == 2
    assert all(not item['escalated_to_curator'] for item in candidates)
    assert by_sample[3]['gap_affected'] is True


def test_streak_reaches_three_escalates_without_gap():
    rows = [_sample(i, i * 10, abnormal=True) for i in range(1, 4)]
    candidates = domain.build_candidates(rows, set())
    last = candidates[-1]
    assert last['consecutive_count'] == 3
    assert last['escalated_to_curator'] is True
    assert last['anomaly_type'] == 'continuous_drift'
    assert last['severity'] == 'critical'
    assert last['status'] == 'escalated'


def test_door_open_overrides_continuous_classification():
    rows = [_sample(i, i * 10, abnormal=True, door=True) for i in range(1, 4)]
    candidates = domain.build_candidates(rows, set())
    assert all(item['anomaly_type'] == 'door_interference' for item in candidates)
    assert all(not item['escalated_to_curator'] for item in candidates)
    assert all(item['severity'] == 'watch' for item in candidates)


# ------------------------------------------------------- 重算计划与血缘

def test_plan_rerun_freezes_closed_events_and_keeps_judgement():
    current = [
        _event(1, status='closed', severity='high'),
        _event(2, status='open', severity='high'),
    ]
    candidates = [
        domain.classify_candidate(_sample(1, 0, abnormal=True), 1),
        domain.classify_candidate(_sample(2, 10, abnormal=True), 1),
    ]
    plan = domain.plan_rerun(current, candidates)
    assert plan['frozen_sample_ids'] == [1]
    assert plan['stats']['frozen'] == 1
    # 关闭事件不产生任何重算操作，判定原样保留
    ops_samples = {op['sample_id'] for op in plan['operations']}
    assert 1 not in ops_samples
    assert 2 in ops_samples


def test_plan_rerun_creates_updates_revokes_and_records_lineage():
    current = [
        _event(2, score=-0.2),                       # 判定会变
        _event(3, score=-0.2),                       # 将被撤销
    ]
    candidates = [
        domain.classify_candidate(_sample(1, 0, abnormal=True, score=-0.3), 1),  # 新增
        domain.classify_candidate(_sample(2, 10, abnormal=True, score=-0.9), 1),  # 更新
    ]
    plan = domain.plan_rerun(current, candidates)
    ops = {op['sample_id']: op for op in plan['operations']}
    assert ops[1]['op'] == 'create'
    assert ops[1]['change_type'] == 'fresh'
    assert ops[2]['op'] == 'update'
    assert ops[2]['change_type'] == 'recalculated'
    assert ops[3]['op'] == 'revoke'
    assert ops[3]['change_type'] == 'revoked'
    assert plan['stats'] == {'created': 1, 'updated': 1, 'revoked': 1, 'unchanged': 0, 'frozen': 0}
    # 血缘包含重算前后快照语义
    assert ops[2]['before']['score'] == -0.2
    assert ops[2]['after']['score'] == -0.9


def test_plan_rerun_unchanged_keeps_judgement():
    candidate = domain.classify_candidate(_sample(1, 0, abnormal=True, score=-0.2), 1)
    current = [_event(1, score=-0.2, reason=candidate['reason'])]
    plan = domain.plan_rerun(current, [candidate])
    assert plan['operations'][0]['op'] == 'unchanged'
    assert plan['operations'][0]['change_type'] == 'unchanged'


def test_escalation_added_and_removed_detection():
    before = _event(1, escalated=False, consecutive_count=2)
    after_escalated = domain.classify_candidate(_sample(1, 0, abnormal=True), 3)
    change, _ = domain.classify_change(before, after_escalated)
    assert change == 'escalation_added'

    before_escalated = _event(1, escalated=True, consecutive_count=3,
                              anomaly_type='continuous_drift', severity='critical',
                              status='escalated')
    after_downgrade = domain.classify_candidate(_sample(1, 0, abnormal=True), 1)
    change, _ = domain.classify_change(before_escalated, after_downgrade)
    assert change == 'escalation_removed'


def test_type_change_detection():
    before = _event(1, anomaly_type='environment_outlier', door_context=False)
    after = _event(1, anomaly_type='door_interference', door_context=True)
    change, _ = domain.classify_change(before, after)
    assert change == 'type_change'


def test_group_merge_detection_when_late_sample_joins_drift_segments():
    # 旧版两段独立异常（组 {1} 与 {4,5}），迟到采样插入后合并成一段 1..5
    old = domain.compute_streak_groups([
        _event(1, consecutive_count=1),
        _event(4, consecutive_count=1),
        _event(5, consecutive_count=2),
    ])
    new = domain.compute_streak_groups([
        _event(i, consecutive_count=i) for i in range(1, 6)
    ])
    marks = domain.diff_groups(old, new)
    assert marks[5] == 'merge'
    assert marks[1] == 'merge'


def test_group_split_detection_when_drift_segment_breaks_apart():
    old = domain.compute_streak_groups([
        _event(i, consecutive_count=i) for i in range(1, 4)
    ])
    new = domain.compute_streak_groups([
        _event(1, consecutive_count=1),
        _event(3, consecutive_count=1),
    ])
    marks = domain.diff_groups(old, new)
    assert marks[3] == 'split'


def test_plan_rerun_marks_merge_on_connected_segments():
    current = [
        _event(1, consecutive_count=1),
        _event(4, consecutive_count=1),
        _event(5, consecutive_count=2),
    ]
    candidates = [
        domain.classify_candidate(_sample(i, i * 10, abnormal=True), i) for i in range(1, 6)
    ]
    plan = domain.plan_rerun(current, candidates)
    ops = {op['sample_id']: op for op in plan['operations']}
    # 迟到的 2、3 号为新增；原 1、4、5 因组合并被标记 merged
    assert ops[2]['change_type'] == 'fresh'
    assert ops[1]['change_type'] == 'merged'
    assert ops[4]['change_type'] == 'merged'
    assert ops[5]['change_type'] == 'merged'
    assert all(op['group_change'] == 'merge' for op in plan['operations']
               if op['sample_id'] in (1, 4, 5))


def test_snapshot_round_trip_contains_all_judgement_fields():
    event = _event(1)
    snap = domain.snapshot(event)
    assert set(snap) == set(domain.EVENT_FIELDS)
    assert snap['anomaly_type'] == 'environment_outlier'
