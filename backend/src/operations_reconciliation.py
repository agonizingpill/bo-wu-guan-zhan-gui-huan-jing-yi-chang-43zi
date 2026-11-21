"""Additional operational reconciliation rules for this project."""

RECONCILIATION_VIEWS = {
    'sensor-evidence-gaps': (
        '传感采样断档定位',
        '按展柜的有效采样时序定位超过现行业务上限 30 分钟的间隔，辅助补采与校验上传。',
        ['展柜', '采样来源', '前次采样', '当前采样', '实际间隔 分', '核查建议'],
        """
WITH ordered AS (
    SELECT case_id,source,sampled_at,LAG(sampled_at) OVER (PARTITION BY case_id ORDER BY sampled_at) AS previous_at
    FROM environment_samples WHERE state='accepted'
)
SELECT c.code,o.source,o.previous_at,o.sampled_at,
       ROUND(EXTRACT(EPOCH FROM (o.sampled_at-o.previous_at))/60,1) AS gap_minutes,
       CASE WHEN o.sampled_at IS NULL THEN '无有效采样'
            WHEN o.previous_at IS NULL THEN '首个有效采样'
            WHEN o.sampled_at-o.previous_at>INTERVAL '30 minutes' THEN '超过 30 分钟，核对缺测'
            ELSE '采样间隔合规' END
FROM display_cases c LEFT JOIN ordered o ON o.case_id=c.id
WHERE c.status <> 'retired' ORDER BY gap_minutes DESC NULLS LAST,c.code,o.sampled_at DESC
""",
    ),
    'environment-recovery-check': (
        '异常处置后环境复测',
        '用处置关闭后的首条有效采样判断温湿回归情况，并区分尚未处置和待采样。',
        ['展柜', '告警编号', '处置状态', '处置时间', '首条复测', '温度', '湿度', '复测判定'],
        """
SELECT c.code,e.id,e.status,e.resolved_at,s.sampled_at,s.temperature,s.humidity,
       CASE WHEN e.resolved_at IS NULL THEN '待完成处置'
            WHEN s.sampled_at IS NULL THEN '待环境复测'
            WHEN s.temperature NOT BETWEEN c.target_temp_min AND c.target_temp_max
              OR s.humidity NOT BETWEEN c.target_rh_min AND c.target_rh_max THEN '复测仍越限'
            ELSE '复测已回归目标' END
FROM anomaly_events e JOIN display_cases c ON c.id=e.case_id
LEFT JOIN LATERAL (SELECT o.sampled_at,o.temperature,o.humidity FROM environment_samples o
                   WHERE o.case_id=e.case_id AND o.state='accepted' AND o.sampled_at>e.resolved_at
                   ORDER BY o.sampled_at LIMIT 1) s ON TRUE
ORDER BY e.created_at DESC,e.id
""",
    ),
    'backfilled-sample-trace': (
        '离线补传采样溯源',
        '展示迟到/补传采样的原始来源、入库时间与采样时间差，重复提交不会产生第二条采样。',
        ['展柜', '采样时间', '入库时间', '迟到 分', '来源', '补传批次', '温度', '湿度', 'CO₂'],
        """
SELECT c.code,s.sampled_at,s.received_at,
       ROUND(EXTRACT(EPOCH FROM (s.received_at-s.sampled_at))/60,1) AS late_minutes,
       s.source,COALESCE(s.backfill_batch,'—'),s.temperature,s.humidity,s.co2_ppm
FROM environment_samples s JOIN display_cases c ON c.id=s.case_id
WHERE s.is_late=TRUE OR s.source IN ('backfill','gateway_backfill')
ORDER BY s.received_at DESC,s.sampled_at DESC
""",
    ),
    'measurement-gap-ledger': (
        '缺测断档台账',
        '显式登记超过 30 分钟的缺测时段与补齐批次；缺测不会被静默填成正常采样。',
        ['展柜', '前次采样', '补后采样', '断档 分', '状态', '发现批次', '补齐批次'],
        """
SELECT c.code,g.previous_at,g.next_at,ROUND(g.gap_seconds/60.0,1),
       CASE WHEN g.state='open' THEN '缺测待补' ELSE '已补齐' END,
       COALESCE(g.detected_batch,'—'),COALESCE(g.filled_batch,'—')
FROM measurement_gaps g JOIN display_cases c ON c.id=g.case_id
ORDER BY g.state DESC,g.previous_at DESC
""",
    ),
    'anomaly-rebuild-lineage': (
        '异常重算前后血缘',
        '记录迟到采样引发的事件新增、撤销、合并、拆分与馆长升级变化，关联重算任务与版本快照。',
        ['展柜', '事件号', '采样时间', '变化类型', '连续组变化', '重算任务', '重算前', '重算后', '说明'],
        """
SELECT c.code,v.event_id,s.sampled_at,v.change_type,v.group_change,
       COALESCE(v.algorithm_run_id::text,'—'),
       CONCAT(COALESCE(v.before_snapshot->>'anomaly_type','—'),'/',
              COALESCE(v.before_snapshot->>'severity','—'),'/',
              COALESCE(v.before_snapshot->>'status','—')),
       CONCAT(COALESCE(v.after_snapshot->>'anomaly_type','—'),'/',
              COALESCE(v.after_snapshot->>'severity','—'),'/',
              COALESCE(v.after_snapshot->>'status','—')),
       COALESCE(v.change_note,'—')
FROM anomaly_event_versions v
JOIN display_cases c ON c.id=v.case_id
JOIN environment_samples s ON s.id=v.sample_id
ORDER BY v.created_at DESC,v.id
""",
    ),
    'rerun-pending-cases': (
        '重算失败待处理展柜',
        '重算失败时旧版结果保留，展柜与算法任务标记待处理，由主管排查后手动重试。',
        ['展柜', '重算状态', '最近失败原因', '最近任务', '未关闭事件'],
        """
SELECT c.code,c.rerun_state,COALESCE(c.last_rerun_error,'—'),
       COALESCE(c.last_analysis_run_id::text,'—'),
       (SELECT COUNT(*) FROM anomaly_events e WHERE e.case_id=c.id
         AND e.revision_status='current' AND e.status<>'closed')
FROM display_cases c
WHERE c.rerun_state='failed_pending' OR c.rerun_state='stale'
ORDER BY c.rerun_state,c.code
""",
    ),
}
