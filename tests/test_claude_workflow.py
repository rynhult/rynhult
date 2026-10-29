"""Evaluate the workflow's actual job gate with hostile and trusted event payloads."""
import json
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize('event_name,author,commenter,is_pr,want', [
    ('issue_comment', 'OWNER', 'OWNER', False, True),
    ('issue_comment', 'CONTRIBUTOR', 'OWNER', False, False),
    ('issue_comment', 'OWNER', 'CONTRIBUTOR', False, False),
    ('issue_comment', 'OWNER', 'OWNER', True, False),
    ('pull_request_review_comment', 'OWNER', 'OWNER', True, False),
    ('issues', 'OWNER', 'OWNER', False, True),
    ('issues', 'CONTRIBUTOR', 'OWNER', False, False),
])
def test_write_job_rejects_pr_and_outsider_content(event_name, author, commenter, is_pr, want):
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/claude.yml').read_text())
    expression = workflow['jobs']['claude']['if'].strip()
    github = {'event_name': event_name, 'event': {
        'issue': {'author_association': author, 'body': '@claude help', 'pull_request': {} if is_pr else None},
        'comment': {'author_association': commenter, 'body': '@claude help'},
    }}
    # GitHub's contains(string, string), boolean operators and property access are JS-compatible here.
    program = 'const github = JSON.parse(process.argv[1]); const contains = (a,b) => a.includes(b);' + (
        'process.stdout.write(JSON.stringify(Boolean(' + expression + ')));')
    result = subprocess.run(['node', '-e', program, json.dumps(github)], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) is want
