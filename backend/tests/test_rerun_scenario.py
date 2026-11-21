"""端到端业务场景：补传迟到采样触发事件合并、升级与关闭冻结。

不依赖 PostgreSQL：用内存事件仓储承载 anomaly_events / anomaly_event_versions，
但重算决策 100% 走生产代码 backfill_domain.plan_rerun，验证完整业务闭环。

运行：cd backend && python3 -m pytest tests/test_rerun_scenario.py -q
"""

from datetime import datetime, timedelta, timezone

from src.services import backfill_domain as domain

T0 = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)


class InMemoryEventStore:
    """模拟 repository.apply_rerun 的落库语义（含冻结与版本留痕）。"""

    def __init__(self):
        self.events = []          # 当前 + 已取代事件全量
        self.versions = []        # 血缘
        self._event_seq = 0
        self._version_seq = 0

    def apply(self, candidates):
        plan = domain.plan_rerun(
            [dict(event) for event in self.events if event['revision_status'] == 'current'],
            candidates,
        )
        for op in plan['operations']:
            if op['op'] == 'create':
                self._event_seq += 1
                event = {
                    'id': self._event_seq,
                    'sample_id': op['sample_id'],
                    'revision_status': 'current',
                    'status': op['after']['status'],
                    **{key: op['after'][key] for key in domain.EVENT_FIELDS},
                }
                self.events.append(event)
            elif op['op'] == 'revoke':
                target = next(e for e in self.events
                              if e['sample_id'] == op['sample_id'] and e['revision_status'] == 'current')
                target['revision_status'] = 'superseded'
            elif op['op'] == 'update':
                target = next(e for e in self.events
                              if e['sample_id'] == op['sample_id'] and e['revision_status'] == 'current')
                target.update({key: op['after'][key] for key in domain.EVENT_FIELDS})
                target['status'] = op['after']['status']
            self._version_seq += 1
            self.versions.append({
                'id': self._version_seq,
                'event_sample': op['sample_id'],
                'change_type': op['change_type'],
                'group_change': op['group_change'],
                'note': op['note'],
                'before': domain.snapshot(op['before']) if op['before'] else {},
                'after': domain.snapshot(op['after']) if op['after'] else {},
            })
        return plan

    def current(self):
        return [e for e in self.events if e['revision_status'] == 'current']

    def close(self, sample_id):
        target = next(e for e in self.current() if e['sample_id'] == sample_id)
        target['status'] = 'closed'


def _abnormal(sample_id, minutes, door=False):
    return {'id': sample_id, 'sampled_at': T0 + timedelta(minutes=minutes),
            'door_open': door, 'abnormal': True, 'score': -0.3, 'threshold': -0.1}


def _candidate(sample_id, streak, door=False):
    return domain.classify_candidate(_abnormal(sample_id, sample_id * 10, door), streak)


def test_late_backfill_merges_segments_and_adds_escalation():
    store = InMemoryEventStore()

    def gap_pairs_of(rows):
        ordered = sorted(rows, key=lambda row: row['id'])
        return {(g.previous_sample_id, g.next_sample_id)
                for g in domain.detect_gaps(
                    [{'id': row['id'], 'sampled_at': row['sampled_at']} for row in ordered])}

    # 第一轮：只有 1、2 号（离群）和 5、6 号（离群）两段，中间 40 分钟缺测互不相连续
    first_rows = [_abnormal(1, 10), _abnormal(2, 20), _abnormal(5, 60), _abnormal(6, 70)]
    store.apply(domain.build_candidates(first_rows, gap_pairs_of(first_rows)))
    before = {e['sample_id']: e for e in store.current()}
    assert len(before) == 4
    assert all(not e['escalated_to_curator'] for e in store.current())

    # 网关恢复后补传迟到的 3、4 号采样，两段被连成连续 5 点
    second_rows = [
        _abnormal(1, 10), _abnormal(2, 20), _abnormal(3, 30),
        _abnormal(4, 40), _abnormal(5, 60), _abnormal(6, 70),
    ]
    plan = store.apply(domain.build_candidates(second_rows, gap_pairs_of(second_rows)))

    after = {e['sample_id']: e for e in store.current()}
    # 新增 2 条，原 4 条保留为当前版本（没有产生重复事件行）
    assert plan['stats']['created'] == 2
    assert plan['stats']['revoked'] == 0
    assert len(store.current()) == 6
    # 连续达到 3 点：3、4、5、6 号全部升级馆长（1、2 号只有 2 点不升级）
    assert not after[1]['escalated_to_curator']
    assert not after[2]['escalated_to_curator']
    assert after[3]['escalated_to_curator'] and after[3]['consecutive_count'] == 3
    assert after[6]['consecutive_count'] == 6
    # 血缘可追溯合并与升级
    change_types = {v['event_sample']: v['change_type'] for v in store.versions}
    assert change_types[3] == 'fresh'
    assert change_types[5] in ('merged', 'escalation_added')
    assert any(v['change_type'] == 'merged' for v in store.versions)


def test_closed_event_is_frozen_even_when_recalc_changes_judgement():
    store = InMemoryEventStore()
    store.apply([_candidate(1, 1)])
    versions_before = len(store.versions)
    store.close(1)  # 主管已关闭，带巡检证据

    # 重算时该样本的判定本应升级，但关闭事件必须冻结
    plan = store.apply([
        domain.classify_candidate({**_abnormal(1, 10), 'score': -0.95}, 3),
    ])
    assert plan['frozen_sample_ids'] == [1]
    assert plan['operations'] == []
    current = store.current()
    assert len(current) == 1
    assert current[0]['status'] == 'closed'
    assert current[0]['escalated_to_curator'] is False  # 原判定保留
    # 冻结事件没有产生任何新血缘版本
    assert len(store.versions) == versions_before


def test_revoked_events_keep_history_as_superseded():
    store = InMemoryEventStore()
    store.apply([_candidate(1, 1), _candidate(2, 1)])
    # 重算后两个采样都不再离群（空候选）
    plan = store.apply([])
    assert plan['stats']['revoked'] == 2
    assert store.current() == []
    revoked = [e for e in store.events if e['revision_status'] == 'superseded']
    assert len(revoked) == 2  # 历史判定仍可追溯，没有物理删除
    revoked_versions = [v for v in store.versions
                        if v['event_sample'] in (1, 2) and v['change_type'] == 'revoked']
    assert len(revoked_versions) == 2


def test_gap_breaks_streak_then_backfill_fills_and_escalation_reappears():
    def ordered(items):
        return sorted(items, key=lambda row: row['sampled_at'])

    # 初始：1、2 号离群，随后缺测，6、7 号离群——不升级
    rows = [_abnormal(1, 0), _abnormal(2, 10), _abnormal(6, 60), _abnormal(7, 70)]
    candidates = domain.build_candidates(ordered(rows), {(2, 6)})
    assert all(not c['escalated_to_curator'] for c in candidates)

    # 补传 3 号后，3→6 仍断档（缺测没有被静默填正常采样），计数不跨段
    rows_with_three = ordered(rows + [_abnormal(3, 20)])
    candidates = domain.build_candidates(rows_with_three, {(3, 6)})
    by_sample = {c['sample_id']: c for c in candidates}
    assert by_sample[3]['consecutive_count'] == 3   # 段内达到 3
    assert by_sample[6]['consecutive_count'] == 1   # 断档后重新计数
    assert by_sample[7]['consecutive_count'] == 2
    assert not by_sample[6]['escalated_to_curator']

    # 缺测时段真正补齐（4、5 号真实采样入库）后，连续段成立，升级恢复
    full_rows = ordered(rows_with_three + [_abnormal(4, 30), _abnormal(5, 40)])
    candidates = domain.build_candidates(full_rows, set())
    escalated = [c for c in candidates if c['escalated_to_curator']]
    assert {c['sample_id'] for c in escalated} == {3, 4, 5, 6, 7}


def test_version_snapshots_capture_before_and_after_judgement():
    store = InMemoryEventStore()
    store.apply([_candidate(1, 1)])
    store.apply([
        domain.classify_candidate({**_abnormal(1, 10), 'score': -0.91}, 1),
    ])
    update_version = next(v for v in store.versions if v['change_type'] == 'recalculated')
    assert update_version['before']['score'] == -0.3
    assert update_version['after']['score'] == -0.91
    assert update_version['note']  # 重算前后关系有文字说明


def test_closed_and_open_events_mix_freezes_only_closed():
    # 同时存在已关闭与未关闭事件，重算只动未关闭部分
    store = InMemoryEventStore()
    store.apply([_candidate(1, 1), _candidate(2, 1), _candidate(3, 1)])
    store.close(2)

    new_candidates = [
        domain.classify_candidate(_abnormal(1, 10), 1),
        domain.classify_candidate({**_abnormal(2, 20), 'score': -0.95}, 2),  # 本应改判
        domain.classify_candidate(_abnormal(3, 30), 1),
        domain.classify_candidate(_abnormal(4, 40), 1),                       # 新增
    ]
    plan = domain.plan_rerun(
        [dict(event) for event in store.events if event['revision_status'] == 'current'],
        new_candidates,
    )
    touched = {op['sample_id'] for op in plan['operations']}
    assert 2 not in touched                       # 关闭事件冻结
    assert 1 in touched and 3 in touched and 4 in touched
    assert plan['frozen_sample_ids'] == [2]
    # 关闭事件的原判定未出现在任何 before 快照中
    assert all((op.get('before') or {}).get('sample_id') != 2 for op in plan['operations'])


def test_repeated_rerun_is_idempotent_on_unchanged_candidates():
    store = InMemoryEventStore()
    candidates = [_candidate(1, 1), _candidate(2, 1)]
    store.apply(candidates)
    plan = store.apply([dict(candidate) for candidate in candidates])
    assert plan['stats'] == {'created': 0, 'updated': 0, 'revoked': 0,
                             'unchanged': 2, 'frozen': 0}
    # 没有产生重复当前事件
    assert len(store.current()) == 2
