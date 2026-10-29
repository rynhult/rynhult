"""One default-on Orchestrator v2 switch; explicit server flags are overrides.

The owner's saved Off remains Off. An explicit master flag pins availability;
legacy slice flags only constrain their slice and cannot bypass the master. Gate
execution still requires its separate host-budget opt-in and trust confirmation.
"""
from acc.env import environment


def enabled() -> bool:
    from ..admin_features import current
    return current('orchestrator', environment.get('ACC_ORCHESTRATOR', '1') == '1')


def slice_enabled(variable):
    return enabled() and environment.get(variable, '1') == '1'


# Compatibility for integrations importing the old internal name. Not a UI label.
preview = enabled
