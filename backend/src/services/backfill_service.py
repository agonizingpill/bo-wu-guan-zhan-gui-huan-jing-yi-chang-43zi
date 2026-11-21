"""离线补传编排与重算排队。

设计要点：
- 补传严格按 sampled_at 入库，UNIQUE(case_id, sampled_at) 保证重复提交不新增采样；
- 补传通道不套用实时录入的 30 分钟拒绝规则，超过 30 分钟的断档进入 measurement_gaps；
- 缺测只登记台账，从不生成虚假正常采样，断档两侧事件标记 gap_affected；
- 有新采样才把展柜置 stale 并排队 backfill_rerun，重算由 worker 异步执行；
- 仓储通过模块级变量注入，测试可替换为内存假实现。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from . import repository

# 可在测试中替换：默认指向 Redis 队列
queue_publisher = None

VALID_SOURCES = {'manual', 'seed', 'backfill', 'gateway_backfill'}


def configure_queue(publisher) -> None:
    global queue_publisher
    queue_publisher = publisher


def _publish_job(job: dict) -> None:
    if queue_publisher is None:
        from ..config.settings import REDIS_URL
        import redis
        client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        client.rpush('museum_case_anomaly_jobs', json.dumps(job))
    else:
        queue_publisher(job)


def validate_sample_payload(payload: dict) -> None:
    required = ('case_id', 'sampled_at', 'temperature', 'humidity', 'co2_ppm')
    for field_name in required:
        if payload.get(field_name) is None:
            raise ValueError(f'缺少必填字段: {field_name}')
    if not (0 <= float(payload['humidity']) <= 100):
        raise ValueError('相对湿度必须在 0-100% 之间')
    if not (-40 <= float(payload['temperature']) <= 80):
        raise ValueError('温度必须在 -40-80°C 设备可测范围内')
    if not (0 <= float(payload['co2_ppm']) <= 10000):
        raise ValueError('CO₂ 必须在 0-10000ppm 设备可测范围内')


def submit_backfill(case_id: int, samples: list[dict], user: dict | None,
                    note: str = '', source: str = 'gateway_backfill') -> dict:
    """提交一批离线补传采样。整批校验、逐条幂等入库。"""
    if source not in VALID_SOURCES:
        raise ValueError(f'不支持的采样来源: {source}')
    if not samples:
        raise ValueError('补传批次至少包含一条采样')

    for payload in samples:
        if int(payload['case_id']) != int(case_id):
            raise ValueError('批次内存在与路径展柜不一致的采样')
        validate_sample_payload(payload)

    # 同一批次内不允许重复时间点
    timestamps = [payload['sampled_at'] for payload in samples]
    if len(set(timestamps)) != len(timestamps):
        raise ValueError('补传批次内存在重复采样时间点，请去重后再提交')

    batch_ref = f'BF-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:8]}'
    user_id = user['id'] if user else None
    batch_id = repository.create_backfill_batch(case_id, batch_ref, user_id, len(samples), note)

    inserted = 0
    duplicates = 0
    received_at = datetime.now(timezone.utc)
    for payload in samples:
        sampled_at = payload['sampled_at']
        is_late = domain_is_late(sampled_at, received_at)
        existing = repository.find_sample(case_id, sampled_at)
        if existing:
            # 重复提交不新增重复采样，也不覆盖原始来源
            duplicates += 1
            continue
        repository.insert_sample(
            case_id, payload, source=source, user_id=user_id,
            is_late=is_late, batch_ref=batch_ref,
        )
        inserted += 1

    all_samples = repository.accepted_samples(case_id)
    gap_result = repository.refresh_gaps(case_id, all_samples, batch_ref)
    repository.update_last_sample_at(case_id)

    run_id = None
    if inserted:
        repository.mark_case_stale(case_id)
        run = repository.create_run(
            case_id,
            f'case-{case_id}-backfill-{uuid.uuid4().hex[:12]}',
            kind='backfill_rerun',
            trigger_source=batch_ref,
            parent_run_id=None,
        )
        run_id = run['id']
        _publish_job({'run_id': run_id, 'case_id': case_id, 'kind': 'backfill_rerun',
                      'batch_ref': batch_ref})

    repository.finish_backfill_batch(
        batch_id, inserted, duplicates,
        gap_result['gap_opened'], gap_result['gap_filled'], run_id,
    )
    return {
        'batch_ref': batch_ref,
        'batch_id': batch_id,
        'submitted': len(samples),
        'inserted': inserted,
        'duplicates': duplicates,
        'gap_opened': gap_result['gap_opened'],
        'gap_filled': gap_result['gap_filled'],
        'rerun_queued': bool(run_id),
        'run_id': run_id,
        'note': note,
    }


def domain_is_late(sampled_at, received_at) -> bool:
    from .backfill_domain import is_late_sample
    return is_late_sample(sampled_at, received_at)


def queue_retry(case_id: int, user: dict | None) -> dict:
    """失败重算的人工重试：只有 failed_pending 状态可以发起。"""
    case = repository.get_case(case_id)
    if not case:
        raise LookupError('展柜不存在')
    if case['rerun_state'] != 'failed_pending':
        raise ValueError('只有标记为待处理（重算失败）的展柜才能发起重试')
    run = repository.create_run(
        case_id,
        f'case-{case_id}-retry-{uuid.uuid4().hex[:12]}',
        kind='retry',
        trigger_source=f"manual-retry:{user['username'] if user else 'system'}",
        parent_run_id=case.get('last_analysis_run_id'),
    )
    _publish_job({'run_id': run['id'], 'case_id': case_id, 'kind': 'retry'})
    return run
