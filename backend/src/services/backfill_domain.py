"""纯领域逻辑：缺测识别、迟到判定、事件重算对账。

本模块不触碰数据库与 sklearn，所有函数都是确定性的纯函数，
供 backfill_service / algorithm 复用，也可用最小样例直接单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 业务规则：同一展柜相邻采样超过 30 分钟即为缺测断档
GAP_LIMIT_SECONDS = 30 * 60

# 连续异常达到该点数升级馆长
ESCALATE_CONSECUTIVE = 3

EVENT_FIELDS = (
    'anomaly_type', 'severity', 'status', 'door_context',
    'consecutive_count', 'escalated_to_curator', 'reason', 'score', 'threshold',
)


@dataclass
class Gap:
    """相邻有效采样之间的缺测断档。"""

    previous_sample_id: int
    next_sample_id: int
    previous_at: Any
    next_at: Any
    gap_seconds: int
    state: str = 'open'

    def to_dict(self) -> dict:
        return {
            'previous_sample_id': self.previous_sample_id,
            'next_sample_id': self.next_sample_id,
            'previous_at': self.previous_at,
            'next_at': self.next_at,
            'gap_seconds': self.gap_seconds,
            'state': self.state,
        }


def detect_gaps(samples: list[dict], limit_seconds: int = GAP_LIMIT_SECONDS) -> list[Gap]:
    """按采样时间排序后，找出所有超过业务上限的相邻间隔。

    缺测只登记台账，不生成任何“正常”采样。samples 需含 id / sampled_at。
    """
    ordered = sorted(samples, key=lambda row: row['sampled_at'])
    gaps: list[Gap] = []
    for previous, current in zip(ordered, ordered[1:]):
        delta = (current['sampled_at'] - previous['sampled_at']).total_seconds()
        if delta > limit_seconds:
            gaps.append(
                Gap(
                    previous_sample_id=previous['id'],
                    next_sample_id=current['id'],
                    previous_at=previous['sampled_at'],
                    next_at=current['sampled_at'],
                    gap_seconds=int(delta),
                )
            )
    return gaps


def is_late_sample(sampled_at, received_at) -> bool:
    """采样时间早于入库时间即视为迟到（补传）数据。"""
    if sampled_at is None or received_at is None:
        return False
    return sampled_at < received_at


def classify_candidate(row: dict, streak: int) -> dict:
    """与 algorithm.py 保持一致的业务分类口径（纯函数版）。"""
    door_context = bool(row['door_open'])
    continuous = streak >= ESCALATE_CONSECUTIVE
    anomaly_type = (
        'door_interference' if door_context
        else ('continuous_drift' if continuous else 'environment_outlier')
    )
    severity = 'watch' if door_context else ('critical' if continuous else 'high')
    escalated = continuous and not door_context
    status = 'escalated' if escalated else 'open'
    reason = (
        '门磁处于开启段，模型异常只作为开门干扰观察，不判定为设备故障'
        if door_context
        else (
            '连续异常达到 3 个采样点，已升级馆长查看'
            if continuous
            else '环境向量偏离展柜稳态基线'
        )
    )
    return {
        'sample_id': row['id'],
        'anomaly_type': anomaly_type,
        'severity': severity,
        'status': status,
        'door_context': door_context,
        'consecutive_count': streak,
        'escalated_to_curator': escalated,
        'reason': reason,
        'score': float(row['score']),
        'threshold': float(row['threshold']),
        'gap_affected': row.get('gap_affected', False),
    }


def build_candidates(rows: list[dict], gap_pairs: set[tuple[int, int]]) -> list[dict]:
    """根据模型离群标签生成候选事件，streak 不跨缺测断档。

    rows 必须按 sampled_at 升序，含 id / door_open / abnormal / score / threshold。
    gap_pairs 为开放缺测的 (前一条采样 id, 后一条采样 id) 集合。
    """
    candidates: list[dict] = []
    streak = 0
    previous_id: int | None = None
    for row in rows:
        crosses_gap = previous_id is not None and (previous_id, row['id']) in gap_pairs
        if crosses_gap:
            streak = 0
        if row['abnormal']:
            streak += 1
        else:
            streak = 0
        if row['abnormal']:
            enriched = dict(row)
            enriched['gap_affected'] = (
                (previous_id, row['id']) in gap_pairs
                or any(row['id'] in pair for pair in gap_pairs)
            )
            candidates.append(classify_candidate(enriched, streak))
        previous_id = row['id']
    return candidates


def snapshot(event: dict) -> dict:
    """提取事件判定字段作为版本快照，datetime/Decimal 由调用方序列化。"""
    return {field: event.get(field) for field in EVENT_FIELDS}


def _same_judgement(before: dict, after: dict) -> bool:
    for field_name in EVENT_FIELDS:
        old_value = before.get(field_name)
        new_value = after.get(field_name)
        if field_name in ('score', 'threshold'):
            if float(old_value) != float(new_value):
                return False
        elif old_value != new_value:
            return False
    return True


def reconcile_events(
    previous_events: list[dict],
    candidates: list[dict],
    old_groups: dict[frozenset, list[int]] | None = None,
    new_groups: dict[frozenset, list[int]] | None = None,
) -> list[dict]:
    """重算对账：对照旧版当前事件与新版候选，产出逐条变更计划。

    返回动作列表，动作类型：create / update / unchanged / revoke。
    合并、拆分、升级变化由 classify_change 进一步标注到血缘表。
    """
    previous_by_sample = {event['sample_id']: event for event in previous_events}
    candidate_by_sample = {candidate['sample_id']: candidate for candidate in candidates}
    actions: list[dict] = []

    for sample_id, candidate in candidate_by_sample.items():
        old = previous_by_sample.get(sample_id)
        if old is None:
            actions.append({'action': 'create', 'sample_id': sample_id, 'after': candidate})
        elif _same_judgement(snapshot(old), candidate):
            actions.append({'action': 'unchanged', 'sample_id': sample_id, 'before': old, 'after': candidate})
        else:
            actions.append({'action': 'update', 'sample_id': sample_id, 'before': old, 'after': candidate})

    for sample_id, old in previous_by_sample.items():
        if sample_id not in candidate_by_sample:
            actions.append({'action': 'revoke', 'sample_id': sample_id, 'before': old})

    actions.sort(key=lambda item: item['sample_id'])
    return actions


def classify_change(before: dict | None, after: dict | None) -> tuple[str, str]:
    """返回 (change_type, group_change)。

    change_type: fresh / recalculated / revoked / merged / split /
                 escalation_added / escalation_removed / type_change / unchanged
    group_change: same / new / merge / split，由调用方按组比对覆盖。
    """
    if before is None and after is not None:
        return 'fresh', 'new'
    if after is None and before is not None:
        return 'revoked', 'same'

    escalation_before = bool(before.get('escalated_to_curator'))
    escalation_after = bool(after.get('escalated_to_curator'))
    if not escalation_before and escalation_after:
        return 'escalation_added', 'same'
    if escalation_before and not escalation_after:
        return 'escalation_removed', 'same'
    if before.get('anomaly_type') != after.get('anomaly_type'):
        return 'type_change', 'same'
    if snapshot(before) != snapshot(after):
        return 'recalculated', 'same'
    return 'unchanged', 'same'


def diff_groups(
    old_groups: dict[frozenset, list[int]],
    new_groups: dict[frozenset, list[int]],
) -> dict[int, str]:
    """比对连续组的 sample_id 集合，返回 sample_id -> merge/split/same 标记。"""
    result: dict[int, str] = {}
    old_index: dict[int, list[frozenset]] = {}
    new_index: dict[int, list[frozenset]] = {}
    for key, members in old_groups.items():
        for sample_id in members:
            old_index.setdefault(sample_id, []).append(key)
    for key, members in new_groups.items():
        for sample_id in members:
            new_index.setdefault(sample_id, []).append(key)

    for sample_id in set(old_index) | set(new_index):
        old_keys = old_index.get(sample_id, [])
        new_keys = new_index.get(sample_id, [])
        old_union = {member for key in old_keys for member in old_groups[key]}
        new_union = {member for key in new_keys for member in new_groups[key]}
        if not old_keys and new_keys:
            result[sample_id] = 'new'
        elif old_keys and not new_keys:
            result[sample_id] = 'same'  # 撤销在 classify_change 已处理
        elif old_union != new_union:
            # 新组比旧组覆盖更多旧样本 -> 合并；旧组被拆开 -> 拆分
            result[sample_id] = 'merge' if len(new_union) > len(old_union) else 'split'
        else:
            result[sample_id] = 'same'
    return result


def compute_streak_groups(events: list[dict]) -> dict[frozenset, list[int]]:
    """把同一连续偏移段的事件归组。

    事件按 sample_id（=时序位置）排序，consecutive_count 回落到 1
    或门开上下文切换时断开为新组。
    """
    ordered = sorted(events, key=lambda event: event['sample_id'])
    groups: dict[frozenset, list[int]] = {}
    current: list[int] = []
    last_count = 0
    last_door: bool | None = None
    for event in ordered:
        count = int(event.get('consecutive_count', 1))
        door = bool(event.get('door_context'))
        if current and (count <= 1 or door != last_door):
            groups[frozenset(current)] = current
            current = []
        current.append(event['sample_id'])
        last_count = count
        last_door = door
    if current:
        groups[frozenset(current)] = current
    return groups


CHANGE_NOTES = {
    'fresh': '重算新增异常',
    'recalculated': '重算分数或判定更新',
    'merged': '迟到采样使相邻异常段合并',
    'split': '迟到采样插入后原异常段拆分',
    'escalation_added': '连续异常达到 3 点，新增馆长升级',
    'escalation_removed': '连续段被迟到采样打断，撤销馆长升级',
    'type_change': '异常类型重算变化',
    'unchanged': '重算前后判定一致',
    'revoked': '重算后该采样不再构成异常',
}


def change_note(change_type: str, before: dict | None, after: dict | None) -> str:
    note = CHANGE_NOTES.get(change_type, change_type)
    if before and after and change_type not in ('fresh', 'revoked'):
        note += (
            f"（{before.get('anomaly_type')}/{before.get('severity')} → "
            f"{after.get('anomaly_type')}/{after.get('severity')}）"
        )
    return note


def plan_rerun(current_events: list[dict], candidates: list[dict]) -> dict:
    """重算决策纯函数：冻结已关闭事件，对其余事件产出操作计划与血缘描述。

    仓储层只负责把计划落库；本函数不触碰任何 IO，可独立单测。
    """
    frozen = [event for event in current_events if event['status'] == 'closed']
    mutable = [event for event in current_events if event['status'] != 'closed']
    frozen_sample_ids = {event['sample_id'] for event in frozen}
    live_candidates = [
        candidate for candidate in candidates
        if candidate['sample_id'] not in frozen_sample_ids
    ]

    old_groups = compute_streak_groups(mutable)
    new_groups = compute_streak_groups(live_candidates)
    group_marks = diff_groups(old_groups, new_groups)
    actions = reconcile_events(mutable, live_candidates)

    operations: list[dict] = []
    stats = {'created': 0, 'updated': 0, 'revoked': 0, 'unchanged': 0, 'frozen': len(frozen)}

    for action in actions:
        sample_id = action['sample_id']
        before = action.get('before')
        after = action.get('after')
        change_type, group_change = classify_change(before, after)
        group_mark = group_marks.get(sample_id, 'same')
        if action['action'] in ('create', 'update') and group_mark in ('merge', 'split'):
            # 血缘枚举使用 merged/split；合并到已有事件记 merged，拆分出的新事件记 fresh
            if group_mark == 'merge':
                change_type = 'merged'
            elif action['action'] == 'update':
                change_type = 'split'
            group_change = group_mark
        note = change_note(change_type, before, after)
        operations.append({
            'op': action['action'],
            'sample_id': sample_id,
            'before': before,
            'after': after,
            'change_type': change_type,
            'group_change': group_change,
            'note': note,
        })
        stats[{'create': 'created', 'update': 'updated', 'revoke': 'revoked',
               'unchanged': 'unchanged'}[action['action']]] += 1

    return {
        'operations': operations,
        'stats': stats,
        'frozen_sample_ids': sorted(frozen_sample_ids),
    }
