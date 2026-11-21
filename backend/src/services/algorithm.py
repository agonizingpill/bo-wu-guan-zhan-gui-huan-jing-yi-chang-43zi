"""异常识别与重算编排。

模型口径保持 README 约定：RobustScaler + IsolationForest(160, 0.22, 1069)。
重算部分把领域对账委托给 backfill_domain，把事务化写入委托给 repository：
- 已关闭事件冻结，模型基线仍包含其采样，但不会被重建或改判；
- 缺测断档打断连续计数，缺测只登记台账，绝不生成虚假正常采样；
- 任何异常向上抛出，由 worker 标记 failed / retry_pending，旧版结果保留。
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from . import backfill_domain as domain
from . import repository
from ..utils.db import clean

RANDOM_STATE = 1069
CONTAMINATION = 0.22


def run_case_analysis(case_id: int, run_id: int) -> tuple[dict, dict]:
    samples = repository.accepted_samples(case_id)
    if len(samples) < 6:
        raise ValueError('至少需要 6 条有效环境采样才能建立异常基线')

    features = np.array([
        [float(row['temperature']), float(row['humidity']), float(row['co2_ppm'])]
        for row in samples
    ])
    scaler = RobustScaler()
    scaled = scaler.fit_transform(features)
    model = IsolationForest(contamination=CONTAMINATION, random_state=RANDOM_STATE, n_estimators=160)
    labels = model.fit_predict(scaled)
    scores = model.decision_function(scaled)
    threshold = float(np.quantile(scores, CONTAMINATION))

    open_gaps = [gap for gap in repository.list_gaps(case_id) if gap['state'] == 'open']
    gap_pairs = {(gap['previous_sample_id'], gap['next_sample_id']) for gap in open_gaps}
    gap_affected_ids = {sample_id for pair in gap_pairs for sample_id in pair}
    rows = []
    for row, label, score in zip(samples, labels, scores):
        rows.append({
            'id': row['id'],
            'door_open': bool(row['door_open']),
            'abnormal': label == -1,
            'score': float(score),
            'threshold': threshold,
            'gap_affected': row['id'] in gap_affected_ids,
        })

    candidates = domain.build_candidates(rows, gap_pairs)
    applied = repository.apply_rerun(case_id, run_id, rows, candidates, gap_affected_ids)

    live_candidates = [
        item for item in candidates
        if item['sample_id'] not in set(applied['frozen_sample_ids'])
    ]
    metrics = {
        'sample_count': len(samples),
        'outlier_count': len(live_candidates),
        'outlier_rate': round(len(live_candidates) / len(samples), 4),
        'threshold': round(threshold, 5),
        'door_interference_count': sum(1 for item in live_candidates if item['anomaly_type'] == 'door_interference'),
        'continuous_upgrade_count': sum(1 for item in live_candidates if item['status'] == 'escalated'),
        'open_gap_count': len([gap for gap in repository.list_gaps(case_id) if gap['state'] == 'open']),
        'rebuild_stats': applied['stats'],
    }
    result = {
        'events': [
            {key: item[key] for key in ('sample_id', 'anomaly_type', 'status', 'score', 'gap_affected')}
            for item in live_candidates
        ],
        'lineage': applied['lineage'],
        'frozen_closed_event_samples': applied['frozen_sample_ids'],
        'feature_order': ['temperature_c', 'relative_humidity_pct', 'co2_ppm'],
        'scaler': 'RobustScaler(median/IQR)',
        'detector': 'IsolationForest(n_estimators=160, contamination=0.22, random_state=1069)',
        'completed_at': datetime.now(timezone.utc).isoformat(),
    }
    repository.finish_run_success(run_id, clean(metrics), clean(result))
    return metrics, result
