"""Regression for native WebKitGTK page width; no production account/services."""
import json
import os
import subprocess

import pytest
from test_native import display
from test_process import ROOT, companion


@pytest.mark.skipif(os.environ.get('DESKTOP_NATIVE_CHECK') != '1', reason='Opt-in WebKitGTK layout qualification')
def test_native_projects_page_uses_available_width(tmp_path):
    with companion(tmp_path) as (_, client, _, _, token), display() as (runtime, address, _):
        result = subprocess.run(['/usr/bin/python3', '-I', str(ROOT / 'desktop/tests/webkit_probe.py')],
            input=json.dumps({'origin': str(client.base_url), 'token': token}), text=True, capture_output=True, timeout=30,
            env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin', 'GDK_BACKEND': 'broadway', 'BROADWAY_DISPLAY': ':1',
                 'XDG_RUNTIME_DIR': str(runtime), 'DBUS_SESSION_BUS_ADDRESS': address, 'AT_SPI_BUS_ADDRESS': address})
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        (ROOT / '.acc/artifacts/desktop-webkit-layout.json').write_text(json.dumps(report, indent=2))
        pages = [e for e in report['elements'] if e['tag'] == 'DIV' and 'mx-auto' in e['cls']]
        assert pages and pages[0]['w'] > 800, report
