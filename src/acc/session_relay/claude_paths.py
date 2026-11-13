"""Fixed launch inputs; a caller cannot select settings, plugin or prompt roots."""
from pathlib import Path

from . import protocol as p

FILES={'settings_path':'settings.json','sdk_settings_path':'relay-sdk-settings.json',
       'mcp_config_path':'mcp.json','plugin_path':'plugin','append_prompt_path':'append-prompt.md'}


def validate(spec, expected_run):
    root=Path(expected_run)
    if not root.is_absolute() or '..' in root.parts or spec.get('run_dir',str(root))!=str(root):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    required={'settings_path'}
    if spec.get('ownership_mode')=='exclusive_idle':required.add('sdk_settings_path')
    for key,name in FILES.items():
        if (key in required or key in spec) and spec.get(key)!=str(root/name):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    return root
