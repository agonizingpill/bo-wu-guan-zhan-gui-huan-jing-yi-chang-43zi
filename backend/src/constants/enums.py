CASE_STATUSES = ['active', 'maintenance', 'retired']
ANOMALY_STATUSES = ['open', 'triaged', 'escalated', 'resolved', 'closed']
ANOMALY_TYPES = ['environment_outlier', 'door_interference', 'continuous_drift']
DUTY_ACTION_TYPES = ['acknowledge', 'verify_door_sealed', 'adjust_climate', 'inspect_sensor', 'call_curator']
CHECK_RESULTS = ['pending', 'pass', 'fail']
SAMPLE_SOURCES = ['manual', 'seed', 'backfill', 'gateway_backfill']
REVISION_STATUSES = ['current', 'superseded']
RERUN_STATES = ['current', 'stale', 'failed_pending']
VERSION_CHANGE_TYPES = [
    'fresh', 'recalculated', 'revoked', 'merged', 'split',
    'escalation_added', 'escalation_removed', 'type_change', 'unchanged',
]
GAP_STATES = ['open', 'filled']
RUN_KINDS = ['analysis', 'backfill_rerun', 'retry']
