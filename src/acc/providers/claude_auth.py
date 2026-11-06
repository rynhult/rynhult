"""Secret-free classification of Claude's documented ``auth status`` output."""
MESSAGE = "Claude sessions signed in with a Claude subscription only take your own typing. Add ANTHROPIC_API_KEY in Settings / Keys and give it to this project to automate them. This switches this project to Claude API billing."
DOCS = '/guide/Providers/Claude'
CODE = 'claude_subscription_automation'
ALLOWED = frozenset({'api_key', 'cloud'})


def classify(status):
    if not isinstance(status, dict) or status.get('loggedIn') is not True:
        return 'unknown'
    method = status.get('authMethod')
    if method in ('api_key', 'api_key_helper'):
        return 'api_key'
    if method == 'third_party':
        return 'cloud'
    if method in ('claude.ai', 'oauth_token'):
        return 'subscription'
    return 'unknown'


def refusal(provider, mode):
    return provider == 'claude' and mode not in ALLOWED


def detail(project_id=None):
    keys = '/settings?claude_api=1' + (f'&project_id={project_id}' if project_id else '') + '#keys'
    return {'code': CODE, 'message': MESSAGE, 'docs': DOCS, 'keys': keys}


# Only these launch credentials may enter an isolated helper or container adapter.
CREDENTIAL_ENV = frozenset({
    'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    'CLAUDE_CODE_USE_FOUNDRY', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN', 'AWS_REGION', 'AWS_DEFAULT_REGION', 'ANTHROPIC_BEDROCK_BASE_URL',
    'ANTHROPIC_VERTEX_PROJECT_ID', 'CLOUD_ML_REGION', 'GOOGLE_CLOUD_PROJECT',
    'GOOGLE_APPLICATION_CREDENTIALS', 'ANTHROPIC_FOUNDRY_API_KEY',
    'ANTHROPIC_FOUNDRY_RESOURCE', 'ANTHROPIC_FOUNDRY_BASE_URL',
})


def credential_env(env):
    return {key: value for key, value in env.items() if key in CREDENTIAL_ENV}


# Auth selectors and credentials which can override the account's Claude login.
# Shared cloud credentials (AWS/Google) remain available to other project tools.
AUTH_ENV = frozenset({
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_FOUNDRY_API_KEY', 'CLAUDE_CODE_USE_BEDROCK',
    'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
})


def subscription_command(argv):
    """Remove inherited billing credentials without adding a shell between tmux and Claude."""
    return ['env', *[arg for key in sorted(AUTH_ENV) for arg in ('-u', key)], *argv]
