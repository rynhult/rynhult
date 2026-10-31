"""Complete server settings for standalone fixtures; Settings() stays production-real for pure default tests."""
from pathlib import Path

from acc.server.config import Settings

REPO = Path(__file__).resolve().parents[1]


def isolated_settings(root: Path, **overrides) -> Settings:
    values = {name: root / folder for name, folder in {
        'data_dir': 'data', 'request_replies_root': 'request-replies', 'run_dir': 'run', 'root_run_dir': 'run-root', 'broker_socket': 'broker.sock',
        'broker_env_dir': 'broker-env', 'accounts_dir': 'accounts', 'updates_dir': 'updates',
        'packages_dir': 'packages', 'containers_dir': 'containers', 'publish_dir': 'publish',
        'agent_home': 'homes/agent', 'catalog_local_dir': 'catalog-local', 'workspaces_root': 'workspaces',
        'hoststate_path': 'hoststate.json', 'git_dir': 'git',
        'containers_record': 'host-qualified.json',  # Preview qualification record: never the host's
    }.items()}
    values.update(update_source=REPO, catalog_dir=REPO / 'catalog', guide_dir=REPO,
                  ruleset_path=REPO / 'deploy/github/ruleset-protect-main.json',
                  hook_path=str(REPO / 'bin/acc-hook'), statusline_path=str(REPO / 'bin/acc-statusline'),
                  static_dir=root / 'static')
    return Settings(**(values | overrides))


def isolated_broker(root: Path, **overrides):
    """Filesystem helper fixtures also get safe values for fields their current method never consumes."""
    import uuid

    from acc.broker.config import BrokerConfig
    values = dict(socket_path=root / 'broker.sock', run_dir=root / 'run',
                  tmux_socket='acc-test-' + uuid.uuid4().hex, allowed_roots=(root,), providers={},
                  hook_path=str(REPO / 'bin/acc-hook'), catalog_dir=REPO / 'catalog',
                  catalog_local_dir=root / 'catalog-local', containers_socket=root / 'containers.sock',
                  containers_attach_root=root / 'containers-attach', container_policy=root / 'container-policy.json')
    return BrokerConfig(**(values | overrides))
