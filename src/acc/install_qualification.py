"""Owner-controlled qualification ceiling, never read from a task checkout at runtime.

Populate only after independent implementation security GO and owner disposable-
host qualification. Keep the registry separate from the adapter code it hashes:
adding a qualified fingerprint must not change that fingerprint recursively.
No environment variable, consent, catalog entry or manager name enables a runtime.
"""

QUALIFIED_TOOL_EXECUTORS = frozenset()
