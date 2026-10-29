#!/usr/bin/python3 -I
"""Installer-only validation with credentials in the child environment, never argv or diagnostics."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

CREDENTIAL_MODULE = Path('/usr/local/lib/acc-dns/credentials.py')


def managed_environment(text):
    if re.fullmatch(r'CF_API_TOKEN=[A-Za-z0-9_-]{1,4096}\n', text):
        token = text.split('=', 1)[1].strip()
        return {'CF_API_TOKEN': token, 'ACC_DNS_CONFIG': 'dns cloudflare ' + json.dumps(token)}
    import importlib.util
    st = CREDENTIAL_MODULE.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise ValueError('credential_parser_refused')
    spec = importlib.util.spec_from_file_location('acc_fixed_dns_credentials', CREDENTIAL_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.environment_values(text)


def main():
    if len(sys.argv) != 5 or sys.argv[1] not in ('adapt', 'validate'):
        return 2
    action, binary, config, credential = sys.argv[1:]
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CF_', 'AWS_', 'AZURE_', 'ACC_DNS_', 'RYNHULT_DNS_'))}
    if os.geteuid() == 0:
        env['HOME'] = '/var/lib/caddy'
        env.pop('XDG_DATA_HOME', None)
        env.pop('XDG_CONFIG_HOME', None)
    if os.path.lexists(credential):
        fd = os.open(credential, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if (st.st_uid != os.geteuid() or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                    stat.S_IMODE(st.st_mode) != 0o600):
                return 1
            text = os.read(fd, 16385).decode()
            if len(text.encode()) > 16384:
                return 1
            env.update(managed_environment(text))
        finally:
            os.close(fd)
    return subprocess.run([binary, action, '--config', config, '--adapter', 'caddyfile'], env=env,
                          stderr=subprocess.DEVNULL, stdout=None if action == 'adapt' else subprocess.DEVNULL,
                          user='caddy' if os.geteuid() == 0 else None,
                          group='caddy' if os.geteuid() == 0 else None,
                          extra_groups=[] if os.geteuid() == 0 else None, timeout=20).returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        sys.exit('Caddy credential/configuration check refused')
