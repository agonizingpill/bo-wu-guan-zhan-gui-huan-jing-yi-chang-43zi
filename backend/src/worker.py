import json
import os
from datetime import datetime, timezone

import redis

from src.config.settings import REDIS_URL
from src.services.algorithm import run_case_analysis
from src.services import repository
from src.utils.db import execute, wait_for_db

QUEUE = 'museum_case_anomaly_jobs'
HEARTBEAT = '/shared/worker-heartbeat'


def heartbeat():
    os.makedirs('/shared', exist_ok=True)
    with open(HEARTBEAT, 'w', encoding='utf-8') as handle:
        handle.write(datetime.now(timezone.utc).isoformat())


def process_job(job: dict) -> None:
    run_id = int(job['run_id'])
    case_id = int(job['case_id'])
    repository.mark_run_running(run_id)
    try:
        run_case_analysis(case_id, run_id)
    except Exception as exc:
        # 重算失败：事务已回滚，旧版事件与巡检证据完整保留，仅标记待处理
        repository.mark_run_failed_pending(run_id, case_id, str(exc))


def main():
    wait_for_db()
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    while True:
        heartbeat()
        item = client.blpop(QUEUE, timeout=5)
        if not item:
            continue
        _, payload = item
        job = json.loads(payload)
        process_job(job)


if __name__ == '__main__':
    main()
