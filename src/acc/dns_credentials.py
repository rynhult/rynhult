"""Standalone fixed schemas and Caddy rendering, shared with the separately installed root helper."""
import json
import re

# Release status is the single source for portal labels and Publishing qualification.
# Promote only after the disposable-zone qualification kit and review. Root Caddy
# binary/per-consumer evidence and policy remain separate execution checks.
PROVIDERS = {
    "cloudflare": {"fields": ["api_token"], "qualification": "qualified"},
    "azure": {"fields": ["tenant_id", "subscription_id", "client_id", "client_secret", "resource_group", "zone_name"], "qualification": "preview"},
    "googleclouddns": {"fields": ["project_id", "managed_zone", "zone_name", "service_account_json"], "qualification": "preview"},
    "digitalocean": {"fields": ["api_token", "zone_name"], "qualification": "preview"},
    "hetzner": {"fields": ["api_token", "zone_name"], "qualification": "preview"},
    "route53": {"fields": ["access_key_id", "secret_access_key", "hosted_zone_id", "zone_name"], "qualification": "preview"},
}


def check_fields(provider: str, fields: dict) -> dict:
    import re
    spec = PROVIDERS.get(provider)
    required = set(spec['fields']) if spec else set()
    optional = {'session_token'} if provider == 'route53' else set()
    if not spec or not required <= set(fields) or set(fields) - required - optional:
        raise ValueError('Supply the fixed provider credential fields')
    for name, value in fields.items():
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or not value.isascii() or any(
                ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Invalid credential field')
        pattern = None
        if name in ('tenant_id', 'subscription_id', 'client_id'):
            pattern = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
        elif name == 'api_token':
            pattern = r'[A-Za-z0-9_-]{8,4096}'
        elif name in ('project_id', 'managed_zone'):
            pattern = r'[a-z][a-z0-9-]{0,62}'
        elif name == 'resource_group':
            pattern = r'[A-Za-z0-9_-]{1,90}'
        elif name in ('access_key_id', 'hosted_zone_id'):
            pattern = r'[A-Z0-9]{1,128}'
        elif name == 'zone_name':
            pattern = r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}'
        if pattern and not re.fullmatch(pattern, value):
            raise ValueError('Invalid credential field')
    if provider == 'googleclouddns':
        check_service_account(fields['service_account_json'], fields['project_id'])
    return dict(fields)


# Exactly the keys of a downloaded service-account key file. Caddy's Go library matches JSON keys
# case-insensitively (last match wins) and honours other credential types, so nothing else may pass.
SERVICE_ACCOUNT = {
    'type': r'service_account', 'project_id': None, 'private_key_id': r'[0-9a-f]{40}',
    'private_key': r'-----BEGIN PRIVATE KEY-----\n[^\x00-\x09\x0b-\x1f]+\n-----END PRIVATE KEY-----\n?',
    'client_email': r'[a-zA-Z0-9._-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com', 'client_id': r'[0-9]{1,32}',
    'auth_uri': re.escape('https://accounts.google.com/o/oauth2/auth'),
    'token_uri': re.escape('https://oauth2.googleapis.com/token'),
    'auth_provider_x509_cert_url': re.escape('https://www.googleapis.com/oauth2/v1/certs'),
    'client_x509_cert_url': re.escape('https://www.googleapis.com/robot/v1/metadata/x509/') + r'[A-Za-z0-9._%-]{1,256}',
    'universe_domain': r'googleapis\.com',
}


def check_service_account(text, project_id):
    def unique(pairs):
        keys = [k for k, _ in pairs]
        if len(set(k.lower() for k in keys)) != len(keys):
            raise ValueError()
        return dict(pairs)
    try:
        account = json.loads(text, object_pairs_hook=unique)
        if (not isinstance(account, dict) or set(account) - set(SERVICE_ACCOUNT) or
                not {'type', 'project_id', 'private_key', 'client_email', 'token_uri'} <= set(account)):
            raise ValueError()
        for key, value in account.items():
            pattern = SERVICE_ACCOUNT[key] or re.escape(project_id)
            if not isinstance(value, str) or not value.isascii() or not re.fullmatch(pattern, value):
                raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise ValueError('Invalid service account') from None


def caddy_provider(provider, fields):
    fields = check_fields(provider, fields)
    if provider == 'cloudflare':
        return {'name': provider, 'api_token': fields['api_token']}
    if provider == 'googleclouddns':
        # A content-addressed path; no caller-supplied filename or ambient ADC.
        import hashlib
        digest = hashlib.sha256(fields['service_account_json'].encode()).hexdigest()
        return {'name': provider, 'gcp_project': fields['project_id'],
                'gcp_application_default': '/etc/caddy/acc-dns-google/' + digest + '.json'}
    if provider == 'digitalocean':
        return {'name': provider, 'auth_token': fields['api_token']}
    if provider == 'hetzner':
        return {'name': provider, 'api_token': fields['api_token']}
    if provider == 'azure':
        return {'name': provider, **{k: fields[k] for k in ('tenant_id', 'subscription_id', 'client_id', 'client_secret')},
                'resource_group_name': fields['resource_group']}
    return {'name': provider, 'region': 'us-east-1', 'access_key_id': fields['access_key_id'],
            'secret_access_key': fields['secret_access_key'], 'hosted_zone_id': fields['hosted_zone_id'],
            **({'session_token': fields['session_token']} if fields.get('session_token') else {})}


def caddy_directive(provider, fields):
    data = caddy_provider(provider, fields)
    name = data.pop('name')
    if name == 'cloudflare':
        return 'dns cloudflare ' + json.dumps(data['api_token'])
    if name == 'digitalocean':
        data = {'api_token': data['auth_token']}
    return 'dns ' + name + ' {\n' + ''.join('  ' + k + ' ' + json.dumps(v) + '\n' for k, v in data.items()) + '}'


def credential_environment(token):
    if isinstance(token, str):
        return f'CF_API_TOKEN={token}\n'
    import base64
    directive = caddy_directive(token['provider'], token['fields'])
    payload = base64.b64encode(json.dumps(token, sort_keys=True).encode()).decode()
    # EnvironmentFile double quotes support literal newlines; escape only quote/backslash.
    quoted = directive.replace('\\', '\\\\').replace('"', '\\"')
    return f'ACC_DNS_PAYLOAD={payload}\nACC_DNS_CONFIG="{quoted}"\n'


def parse_environment(content):
    if re.fullmatch(r'CF_API_TOKEN=[A-Za-z0-9_-]{8,4096}\n', content):
        return content[len('CF_API_TOKEN='):].strip()
    import base64
    try:
        line = content.splitlines()[0]
        if not line.startswith('ACC_DNS_PAYLOAD='):
            raise ValueError()
        token = json.loads(base64.b64decode(line.split('=', 1)[1], validate=True))
        if set(token) != {'provider', 'fields'} or token['provider'] not in PROVIDERS or token['provider'] == 'cloudflare':
            raise ValueError()
        check_fields(token['provider'], token['fields'])
        if content != credential_environment(token):
            raise ValueError()
        return token
    except (ValueError, TypeError, KeyError, IndexError):
        raise ValueError('invalid_credential_file') from None


def environment_values(content):
    token = parse_environment(content)
    if isinstance(token, str):
        return {'CF_API_TOKEN': token, 'ACC_DNS_CONFIG': caddy_directive('cloudflare', {'api_token': token})}
    return {'ACC_DNS_CONFIG': caddy_directive(token['provider'], token['fields'])}
