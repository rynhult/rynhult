"""Per-target schedules and transparent transfer scenarios, never provider billing."""
import math

DAILY = 86400
WEEKLY = 7 * DAILY
DEFAULTS = {'schedule_seconds': DAILY, 'verify_interval_seconds': WEEKLY}


def defaults(target):
    # Before these fields existed the cadence was hard-coded, not owner-configurable.
    # An explicitly stored interval is always retained, including hourly/verify-every-time.
    return {**DEFAULTS, **target}


def full_due(target, result, now):
    interval = defaults(target)['verify_interval_seconds']
    return not result.get('restore_tested_at') or now - result['restore_tested_at'] >= interval


def estimate(target, result):
    target = defaults(target)
    captures = 30 * DAILY / target['schedule_seconds']
    full = captures if not target['verify_interval_seconds'] else min(captures, 30 * DAILY / target['verify_interval_seconds'])
    size = result.get('bytes')
    repo = result.get('repository_bytes', size)
    uploaded = result.get('uploaded_bytes')
    objects = result.get('retention_objects')
    return {'captures':captures, 'full_verifications':full,
            'upload_bytes':None if uploaded is None else uploaded * captures,
            'download_bytes':None if size is None or repo is None else (repo + size) * full,
            # Native request scenario calibrated by tests/backup_cost_meter.py (8 MiB,
            # two snapshots). Service identity/history/probe calls and larger inventories add more.
            'restic_requests':None if target.get('kind') not in ('node','s3') else
                captures * 30 + full * (18 if target['kind']=='node' else 17),
            's3_policy_requests':None if target.get('kind') != 's3' or objects is None else
                captures * (4 * objects + 2 * max(1, math.ceil(objects/1000)) + 7),
            'basis':'30-day scenario: last incremental packed bytes; full checks read the repository plus one snapshot. '
                    'Snapshot input size substitutes for unknown stored size; compression, history growth and metadata change actual traffic. '
                    'Native request counts use an 8 MiB two-snapshot fixture, not target traffic. '
                    'S3 policy requests exclude native restic, setup, retries and maintenance.'}
