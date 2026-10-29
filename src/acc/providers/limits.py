"""Conservative plan-limit screen signals, shared by broker and controller."""
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LIMITS = {
    'claude': r"(?:you.ve hit your limit|usage limit reached)",
    'codex': r"(?:you(?: have|.ve) hit your usage limit|usage limit reached|usage_limit_reached)",
    'copilot': r"(?:exhausted your premium requests|used up your premium request allowance|no premium requests remaining|AI credits.*exhausted)",
}
UNSUPPORTED = 'This provider has no reliable plan-limit signal in Rynhult.'


def capability(provider):
    return {'autoresume': provider in LIMITS, 'reason': None if provider in LIMITS else UNSUPPORTED}


def limit_signal(provider, text):
    if not re.search(LIMITS.get(provider, r'(?!)'), text, re.I):
        return None
    reset = None
    match = re.search(r'\b(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d))', text)
    if match:
        try:
            reset = datetime.fromisoformat(match[1].replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    relative = re.search(r'(?:resets? in|try again in)\s*(\d+)\s*(minutes?|hours?|seconds?)', text, re.I)
    if reset is None and relative:
        reset = time.time() + int(relative[1]) * (3600 if relative[2].lower().startswith('hour') else 60 if relative[2].lower().startswith('minute') else 1)
    local = re.search(r'resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)', text, re.I)
    if reset is None and local:
        try:
            zone = ZoneInfo(local[4])
            current = datetime.fromtimestamp(time.time(), zone)
            hour = int(local[1]) % 12 + (12 if local[3].lower() == 'pm' else 0)
            target = current.replace(hour=hour, minute=int(local[2] or 0), second=0, microsecond=0)
            if target <= current:
                target += timedelta(days=1)
            reset = target.timestamp()
        except (ValueError, ZoneInfoNotFoundError):
            pass
    # Codex renders this clock in the CLI machine's local timezone. Parsing happens
    # in that same account's broker, not in the dashboard machine or browser.
    clock = re.search(r'try again at\s+(\d{1,2}):(\d{2})\s*(am|pm)\b', text, re.I)
    if reset is None and clock:
        try:
            current = datetime.fromtimestamp(time.time()).astimezone()
            hour = int(clock[1]) % 12 + (12 if clock[3].lower() == 'pm' else 0)
            target = current.replace(hour=hour, minute=int(clock[2]), second=0, microsecond=0)
            if target <= current:
                target += timedelta(days=1)
            reset = target.timestamp()
        except ValueError:
            pass
    return {'reset': reset}
