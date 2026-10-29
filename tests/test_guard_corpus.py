"""Desired policy, including known failures catalogued in docs/reviews/guard-gaps.md.

Shell inputs are data, never executed. Expectations describe intended behavior,
not implementation snapshots; known mismatches intentionally remain failures.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "bin" / "acc-guard"
loader = importlib.machinery.SourceFileLoader("acc_guard_corpus", str(GUARD))
spec = importlib.util.spec_from_loader("acc_guard_corpus", loader)
guard = importlib.util.module_from_spec(spec)
loader.exec_module(guard)

POLICY = {
    "workspace": "/srv/ws",
    "rules": {"force_push": True, "push_default": True, "rm_outside": True, "secrets": True, "merge_pr": True},
    "custom": [],
}

# No remote configuration is consulted. Implicit pushes use these branches;
# -C is resolved against /srv/ws, just as check_push resolves it.
BRANCHES = {
    "/srv/ws": "main",
    "/srv/ws/main": "main",
    "/srv/ws/master": "master",
    "/srv/ws/feature": "feature/topic",
    "/srv/ws/sub": "feature/topic",
}

COMMAND_CASES = [
    ("d14-script-log-secret", "script .env", "secrets"),
    ("d14-su-login", "su -lc 'rm -rf build' builder", "rm_outside"),
    ("d14-flock-option-command", "flock -c 'git push origin main' /tmp/guard.lock", "push_default"),
    ("d14-flock-equals", "flock --command='git push -f origin topic' /tmp/guard.lock", "force_push"),
    ("d14-flock-git-config", "flock /tmp/guard.lock git -c color.ui=false push origin main", "push_default"),
    ("d14-flock-nested-shell", "flock /tmp/guard.lock sh -c 'git push -f origin topic'", "force_push"),
    ("d14-subtree-main", "git subtree push --prefix=lib origin main", "push_default"),
    ("d14-subtree-master", "git subtree --prefix lib push origin master", "push_default"),
    ("d14-subtree-local", "git subtree push -P lib origin topic", None),
    ("d14-subtree-other", "git subtree pull --prefix lib origin main", None),
    ("d14-alias", "git -c alias.p=push p origin main", "force_push"),
    ("d14-alias-shell", "git -calias.p='!cat .env' p", "force_push"),
    ("d14-alias-env", "git --config-env=alias.p=ALIAS p", "force_push"),
    ("d14-config-ordinary", "git -c color.ui=false status", None),
    ("d14-find", r"find . -exec git push -f origin topic \;", "force_push"),
    ("d14-find-dir", r"find . -execdir rm -rf relative \;", "rm_outside"),
    ("d14-find-ok", r"find . -ok git push origin main \;", "push_default"),
    ("d14-script", "script -qc 'git push -f origin topic' /dev/null", "force_push"),
    ("d14-su", "su builder -c 'rm -rf /opt/example'", "rm_outside"),
    ("d14-flock", "flock -x /tmp/guard.lock git push origin main", "push_default"),
    ("d14-flock-script", "flock /tmp/guard.lock -c 'cat .env'", "secrets"),
    ("d14-busybox", "busybox rm -rf /opt/example", "rm_outside"),
    ("d14-runner-local", "busybox rm -rf build", None),
    ("d14-find-text", "find . -name 'git push -f'", None),
    ("d14-dynamic-rm", "R=rm; $R -rf /opt/example", "rm_outside"),
    ("d14-substitution-rm", "$(printf rm) -rf /opt/example", "rm_outside"),
    ("d14-dynamic-git", "$GIT push -f origin topic", "force_push"),
    ("d14-dynamic-main", "$GIT push origin main", "push_default"),
    ("d14-dynamic-secret", "$READER .env", "secrets"),
    ("d14-dynamic-local", "$RUN -r build", None),
    ("d14-force-prefix", "git push --force-w origin topic", "force_push"),
    ("d14-mirror-prefix", "git push --mirro origin", "push_default"),
    ("d14-rm-prefix", "rm --rec -f /opt/example", "rm_outside"),
    ("d14-repo-prefix", "git -C feature push --rep=origin main", "push_default"),
    ("d14-no-force-prefix", "git push --no-for origin topic", None),
    ("d14-rm-prefix-local", "rm --rec build", None),
    ("force-long", "git push --force origin feature", "force_push"),
    ("force-short", "git push -f origin feature", "force_push"),
    ("force-lease", "git push --force-with-lease origin feature", "force_push"),
    ("force-lease-ref", "git push --force-with-lease=refs/heads/feature:abc origin feature", "force_push"),
    ("force-includes", "git push --force-if-includes origin feature", "force_push"),
    ("force-combined", "git push -uf origin feature", "force_push"),
    ("force-combined-reverse", "git push -fu origin feature", "force_push"),
    ("force-combined-verbose", "git push -vuf origin feature", "force_push"),
    ("force-after-ref", "git push origin feature -f", "force_push"),
    ("force-quoted", "git push '--force' origin feature", "force_push"),
    ("force-split-quote", "git pu'sh' --for'ce' origin feature", "force_push"),
    ("force-tabs", "\tgit\tpush\t-f\torigin\tfeature\t", "force_push"),
    ("force-spaces", "  git    push   --force   origin feature  ", "force_push"),
    ("force-absolute-git", "/usr/bin/git push -f origin feature", "force_push"),
    ("force-sudo", "sudo git push -f origin feature", "force_push"),
    ("force-env", "env GIT_TRACE=1 git push -f origin feature", "force_push"),
    ("force-assignment", "GIT_TRACE=1 git push -f origin feature", "force_push"),
    ("force-command", "command git push -f origin feature", "force_push"),
    ("force-exec", "exec git push -f origin feature", "force_push"),
    ("force-nohup", "nohup git push -f origin feature", "force_push"),
    ("force-time", "time git push -f origin feature", "force_push"),
    ("force-global-c", "git -c color.ui=false push -f origin feature", "force_push"),
    ("force-global-directory", "git -C sub push -f origin feature", "force_push"),
    ("force-global-equals", "git --git-dir=.git push -f origin feature", "force_push"),
    ("force-refspec", "git push origin +feature:feature", "force_push"),
    ("force-refspec-head", "git push origin +HEAD:refs/heads/topic", "force_push"),
    ("force-second-refspec", "git push origin topic +other:other", "force_push"),
    ("force-repo-option-refspec", "git -C feature push --repo=origin +topic:topic", "force_push"),
    ("force-and", "git status && git push -f origin feature", "force_push"),
    ("force-or", "false || git push -f origin feature", "force_push"),
    ("force-semicolon", "git status; git push -f origin feature", "force_push"),
    ("force-pipe", "git push -f origin feature | tee output.log", "force_push"),
    ("force-newline", "git status\ngit push -f origin feature", "force_push"),
    ("force-background", "git push -f origin feature & wait", "force_push"),
    ("force-comment", "git push -f origin feature # reviewed", "force_push"),
    ("force-sudo-user", "sudo -u builder git push -f origin feature", "force_push"),
    ("force-sudo-noninteractive", "sudo -n git push -f origin feature", "force_push"),
    ("force-env-ignore", "env -i git push -f origin feature", "force_push"),
    ("force-env-unset", "env -u GIT_DIR git push -f origin feature", "force_push"),
    ("force-command-path", "command -p git push -f origin feature", "force_push"),
    ("force-time-format", "time -p git push -f origin feature", "force_push"),
    ("force-subshell", "(git push -f origin feature)", "force_push"),
    ("force-braces", "{ git push -f origin feature; }", "force_push"),
    ("force-shell-script", "sh -c 'git push -f origin feature'", "force_push"),
    ("force-substitution", "echo \"$(git push -f origin feature)\"", "force_push"),
    ("force-backticks", "echo `git push -f origin feature`", "force_push"),
    ("force-continuation", "git push \\\n --force origin feature", "force_push"),
    ("force-shell-heredoc", "sh <<'SH'\ngit push -f origin feature\nSH", "force_push"),
    ("force-before-default", "git push -f origin main", "force_push"),
    # seen live (overnight 2026-10-22, before the shell-aware parser): a Python heredoc's source is data, not shell
    ("allow-python-heredoc-text", "python3 - <<'EOF'\ns = 'workspace_changes(config, ws, \"HEAD; rm -rf /\")'\nEOF", None),
    ("allow-python-heredoc-push", "python3 - <<'EOF'\nprint('git push -f origin main')\nEOF", None),
    ("allow-feature", "git push origin feature", None),
    ("allow-feature-upstream", "git push -u origin feature/topic", None),
    ("allow-tag", "git push origin refs/tags/main", None),
    ("allow-tags", "git push --tags origin", None),
    ("allow-main-source", "git push origin main:feature", None),
    ("allow-master-source", "git push origin master:feature", None),
    ("allow-main-substring", "git push origin feature/main", None),
    ("allow-main-case", "git push origin Main", None),
    ("allow-force-option-after-double-dash", "git push -- origin feature:--force", None),
    ("allow-no-force", "git push --no-force origin feature", None),
    ("allow-lease-disabled", "git push --no-force-with-lease origin feature", None),
    ("allow-main-remote", "git push main feature", None),
    ("allow-force-echo", "echo 'git push -f origin feature'", None),
    ("allow-force-printf", "printf '%s\\n' 'git push --force origin feature'", None),
    ("allow-force-comment", "# git push -f origin feature", None),
    ("allow-force-help", "git help push", None),
    ("allow-force-config", "git config alias.publish 'push --force origin feature'", None),
    ("allow-force-grep", "grep 'git push -f' README.md", None),
    ("allow-force-quoted-chain", "echo 'note; git push -f origin feature'", None),
    ("allow-force-quoted-and", "printf '%s' 'note && git push -f origin feature'", None),
    ("allow-force-quoted-pipe", "echo 'note | git push -f origin feature'", None),
    ("allow-force-comment-chain", "# note; git push -f origin feature", None),
    ("allow-force-heredoc", "cat <<'TEXT'\ngit push -f origin feature\nTEXT", None),
    ("allow-force-single-substitution", "echo '$(git push -f origin feature)'", None),
    ("default-main", "git push origin main", "push_default"),
    ("default-master", "git push origin master", "push_default"),
    ("default-head-main", "git push origin HEAD:main", "push_default"),
    ("default-head-master", "git push origin HEAD:master", "push_default"),
    ("default-full-ref", "git push origin HEAD:refs/heads/main", "push_default"),
    ("default-full-master", "git push origin HEAD:refs/heads/master", "push_default"),
    ("default-full-source", "git push origin refs/heads/topic:refs/heads/main", "push_default"),
    ("default-delete", "git push origin :main", "push_default"),
    ("default-delete-option", "git push --delete origin master", "push_default"),
    ("default-multiple", "git push origin topic HEAD:main", "push_default"),
    ("default-all", "git push --all origin", "push_default"),
    ("default-mirror", "git push --mirror origin", "push_default"),
    ("default-implicit", "git push", "push_default"),
    ("default-remote-only", "git push origin", "push_default"),
    ("default-head", "git push origin HEAD", "push_default"),
    ("default-at", "git push origin @", "push_default"),
    ("default-C-master", "git -C master push origin HEAD", "push_default"),
    ("default-C-main", "git -C main push origin", "push_default"),
    ("default-C-absolute", "git -C /srv/ws/master push origin @", "push_default"),
    ("default-global-worktree", "git --work-tree /srv/ws push origin main", "push_default"),
    ("default-global-gitdir", "git --git-dir /srv/ws/.git push origin main", "push_default"),
    ("default-global-namespace", "git --namespace example push origin main", "push_default"),
    ("default-global-many", "git -C sub -c color.ui=false --no-pager push origin main", "push_default"),
    ("default-global-config-env", "git --config-env=core.editor=EDITOR push origin main", "push_default"),
    ("default-global-compact-C", "git -Cmaster push origin HEAD", "push_default"),
    ("default-push-option", "git push -o ci.skip origin main", "push_default"),
    ("default-receive-pack", "git push --receive-pack git-receive-pack origin main", "push_default"),
    ("default-repo-equals", "git push --repo=origin main", "push_default"),
    ("default-repo-feature-cwd", "git -C feature push --repo=origin main", "push_default"),
    ("default-no-remote", "git push main", "push_default"),
    ("default-matching", "git push origin :", "push_default"),
    ("default-wildcard", "git push origin 'refs/heads/*:refs/heads/*'", "push_default"),
    ("default-sudo-user", "sudo -u builder git push origin main", "push_default"),
    ("default-subshell", "( git push origin main )", "push_default"),
    ("default-shell", "bash -c 'git push origin master'", "push_default"),
    ("default-chain", "git status && git push origin main", "push_default"),
    ("default-quoted-branch", "git push origin 'main'", "push_default"),
    ("default-quoted-command", "'git' 'push' origin master", "push_default"),
    ("default-whitespace", " git  push\torigin\tmain ", "push_default"),
    ("allow-default-C-feature", "git -C feature push origin", None),
    ("allow-default-C-feature-head", "git -C sub push origin HEAD", None),
    ("allow-default-C-feature-at", "git -C feature push origin @", None),
    ("allow-default-C-compact", "git -Cfeature push origin", None),
    ("allow-default-remote-main", "git -C feature push main", None),
    ("allow-default-delete-topic", "git push --delete origin topic", None),
    ("allow-default-refmap", "git push origin main:topic master:other", None),
    ("allow-default-tag-master", "git push origin refs/tags/master", None),
    ("allow-default-echo", "echo 'git push origin main'", None),
    ("allow-default-double-echo", 'echo "git push origin master"', None),
    ("allow-default-branch-argument", "printf '%s' main master", None),
    ("allow-default-quoted-semicolon", "echo 'note; git push origin main'", None),
    ("allow-default-quoted-newline", "printf '%s' 'note\ngit push origin master'", None),
    ("allow-default-heredoc", "cat <<'TEXT'\ngit push origin main\nTEXT", None),
    ("allow-default-comment-chain", "echo ok # note; git push origin main", None),
    ("rm-root", "rm -rf /", "rm_outside"),
    ("rm-root-glob", "rm -rf /*", "rm_outside"),
    ("rm-etc", "rm -r /etc/example", "rm_outside"),
    ("rm-long", "rm --recursive /opt/example", "rm_outside"),
    ("rm-uppercase", "rm -R /opt/example", "rm_outside"),
    ("rm-combined-reverse", "rm -fr /opt/example", "rm_outside"),
    ("rm-combined-verbose", "rm -vfr /opt/example", "rm_outside"),
    ("rm-separated", "rm -f -r /opt/example", "rm_outside"),
    ("rm-flags-last", "rm /opt/example -rf", "rm_outside"),
    ("rm-double-dash", "rm -rf -- /opt/example", "rm_outside"),
    ("rm-tilde", "rm -rf ~/projects", "rm_outside"),
    ("rm-home", "rm -rf $HOME/projects", "rm_outside"),
    ("rm-home-braces", "rm -rf ${HOME}/projects", "rm_outside"),
    ("rm-variable", "rm -rf $TARGET", "rm_outside"),
    ("rm-parent", "rm -rf ../other", "rm_outside"),
    ("rm-parent-normalized", "rm -rf build/../../other", "rm_outside"),
    ("rm-prefix-sibling", "rm -rf /srv/ws-other/build", "rm_outside"),
    ("rm-workspace-itself", "rm -rf /srv/ws", "rm_outside"),
    ("rm-current-root", "rm -rf .", "rm_outside"),
    ("rm-parent-root", "rm -rf ..", "rm_outside"),
    ("rm-mixed-targets", "rm -rf build /etc/example", "rm_outside"),
    ("rm-quoted-space", "rm -rf '/opt/old project'", "rm_outside"),
    ("rm-escaped-space", "rm -rf /opt/old\\ project", "rm_outside"),
    ("rm-sudo", "sudo rm -rf /opt/example", "rm_outside"),
    ("rm-env", "env KEEP=1 rm -rf /opt/example", "rm_outside"),
    ("rm-absolute", "/bin/rm -rf /opt/example", "rm_outside"),
    ("rm-chain", "echo start && rm -rf /opt/example", "rm_outside"),
    ("rm-pipe", "printf x | rm -rf /opt/example", "rm_outside"),
    ("rm-sudo-user", "sudo -u builder rm -rf /opt/example", "rm_outside"),
    ("rm-env-ignore", "env -i rm -rf /opt/example", "rm_outside"),
    ("rm-subshell", "(rm -rf /opt/example)", "rm_outside"),
    ("rm-shell", "sh -c 'rm -rf /opt/example'", "rm_outside"),
    ("rm-cd-escape", "cd /opt && rm -rf example", "rm_outside"),
    ("rm-subshell-cd", "(cd /opt; rm -rf example)", "rm_outside"),
    ("rm-tmp-parent", "rm -rf /tmp/../etc/example", "rm_outside"),
    ("rm-var-tmp-parent", "rm -rf /var/tmp/../../etc/example", "rm_outside"),
    ("allow-rm-build", "rm -rf build", None),
    ("allow-rm-many", "rm -rf build dist node_modules", None),
    ("allow-rm-absolute", "rm -rf /srv/ws/build", None),
    ("allow-rm-normalized", "rm -rf build/../dist", None),
    ("allow-rm-quoted-space", "rm -rf 'old build'", None),
    ("allow-rm-escaped-space", "rm -rf old\\ build", None),
    ("allow-rm-temp", "rm -rf /tmp/guard-scratch", None),
    ("allow-rm-var-temp", "rm -rf /var/tmp/guard-scratch", None),
    ("allow-rm-no-recursion", "rm /etc/example", None),
    ("allow-rm-force-only", "rm -f /etc/example", None),
    ("allow-rm-unlink", "unlink /opt/example", None),
    ("allow-rm-local-glob", "rm -rf build/*", None),
    ("allow-rm-glob", "rm -rf *", None),
    ("allow-rm-dot-glob", "rm -rf .*", None),
    ("allow-rm-literal-tilde", "rm -rf '~/projects'", None),
    ("allow-rm-literal-home", "rm -rf '$HOME/projects'", None),
    ("allow-rm-dash-target", "rm -- -rf /opt/example", None),
    ("allow-rm-echo", "echo 'rm -rf /opt/example'", None),
    ("allow-rm-quoted-chain", "echo 'example; rm -rf /opt/example'", None),
    ("allow-rm-heredoc", "cat <<'TEXT'\nrm -rf /opt/example\nTEXT", None),
    ("allow-rm-comment", "# rm -rf /opt/example", None),
    ("allow-rm-comment-chain", "# note; rm -rf /opt/example", None),
    ("secret-env", "cat .env", "secrets"),
    ("secret-env-local", "cat .env.local", "secrets"),
    ("secret-env-production", "cat config/.env.production", "secrets"),
    ("secret-head", "head -n 5 .env", "secrets"),
    ("secret-tail", "tail .env", "secrets"),
    ("secret-less", "less .env", "secrets"),
    ("secret-cp", "cp .env /tmp/example", "secrets"),
    ("secret-grep", "grep TOKEN .env", "secrets"),
    ("secret-sed", "sed -n '1p' .env", "secrets"),
    ("secret-awk", "awk '{print $1}' .env", "secrets"),
    ("secret-netrc", "cat ~/.netrc", "secrets"),
    ("secret-git-credentials", "cat $HOME/.git-credentials", "secrets"),
    ("secret-pgpass", "cat ${HOME}/.pgpass", "secrets"),
    ("secret-rsa", "cat ~/.ssh/id_rsa", "secrets"),
    ("secret-ed25519", "cat ~/.ssh/id_ed25519", "secrets"),
    ("secret-pem", "cat config/client.pem", "secrets"),
    ("secret-p12", "base64 config/client.p12", "secrets"),
    ("secret-pfx", "xxd config/client.pfx", "secrets"),
    ("secret-service-account", "cat service-account-demo.json", "secrets"),
    ("secret-credentials-json", "cat credentials.json", "secrets"),
    ("secret-aws", "cat ~/.aws/credentials", "secrets"),
    ("secret-docker", "cat ~/.docker/config.json", "secrets"),
    ("secret-gh", "cat ~/.config/gh/hosts.yml", "secrets"),
    ("secret-codex", "cat ~/.codex/auth.json", "secrets"),
    ("secret-claude", "cat ~/.claude/.credentials.json", "secrets"),
    ("secret-copilot", "cat ~/.copilot/config.json", "secrets"),
    ("secret-cursor", "cat ~/.config/cursor/auth.json", "secrets"),
    ("secret-kube", "cat ~/.kube/config", "secrets"),
    ("secret-directory", "cat ~/.gnupg/private-keys-v1.d/example.key", "secrets"),
    ("secret-acc-store", "cat /srv/acc/data/secrets/example", "secrets"),
    ("secret-quoted", "cat '.env'", "secrets"),
    ("secret-whitespace", " cat\t.env ", "secrets"),
    ("secret-chain", "echo start && cat .env", "secrets"),
    ("secret-input-redirection", "cat < .env", "secrets"),
    ("secret-attached-redirection", "cat <.env", "secrets"),
    ("secret-aws-redirection", "cat <~/.aws/credentials", "secrets"),
    ("secret-redirect-only", "<.env", "secrets"),
    ("secret-relative-ssh", "cat .ssh/config", "secrets"),
    ("secret-dot-component", "cat /home/demo/.aws/./credentials", "secrets"),
    ("secret-normalize-file", "cat /home/demo/.aws/nested/../credentials", "secrets"),
    ("secret-subshell", "(cat ~/.aws/credentials)", "secrets"),
    ("secret-shell", "sh -c 'cat ~/.aws/credentials'", "secrets"),
    ("secret-file-option", "grep --file=.env README.md", "secrets"),
    ("secret-source", ". .env", "secrets"),
    ("secret-source-builtin", "source .env", "secrets"),
    ("secret-session-env", 'cat "$ACC_RUN_DIR/session.env"', "secrets"),
    ("secret-session-env-braces", "grep KEY ${ACC_RUN_DIR}/session.env", "secrets"),
    ("secret-session-env-source", ". /srv/acc/run/agent/0123456789ab/session.env", "secrets"),
    ("allow-key-by-variable", 'curl -H "Authorization: Bearer $OPENAI_API_KEY" https://api.example.com/v1/models', None),
    ("allow-secret-example", "cat .env.example", None),
    ("allow-secret-sample", "cat .env.sample", None),
    ("allow-secret-template", "cat .env.template", None),
    ("allow-secret-dist", "cat .env.dist", None),
    ("allow-secret-defaults", "cat .env.defaults", None),
    ("allow-secret-public-key", "cat ~/.ssh/id_ed25519.pub", None),
    ("allow-secret-source-code", "cat src/app.py", None),
    ("allow-secret-suffix", "cat .environment", None),
    ("allow-secret-backup-example", "cat docs/env.example.txt", None),
    ("allow-secret-echo", "echo .env", None),
    ("allow-secret-quoted-echo", "echo '.env'", None),
    ("allow-secret-home-echo", "echo '~/.aws/credentials'", None),
    ("allow-secret-printf", "printf '%s\\n' '.env.production'", None),
    ("allow-secret-path-variable", "CONFIG_PATH=.env echo configured", None),
    ("allow-secret-comment", "# cat .env", None),
    ("allow-secret-quoted-chain", "echo 'example; cat .env'", None),
    ("allow-secret-heredoc", "cat <<'TEXT'\ncat .env\nTEXT", None),
    ("allow-secret-here-string", "cat <<< '.env'", None),
    ("allow-secret-search-text", "grep -F '.env' README.md", None),
    ("allow-secret-relative-normalized", "cat /home/demo/.ssh/../notes.txt", None),
    ("allow-secret-input-normalized", "cat config/.env/../notes.txt", None),
    ("allow-secret-filename-output", "printf '%s\\n' ~/.ssh/id_rsa", None),
    ("allow-empty", "", None),
    ("allow-whitespace", " \t\n ", None),
    ("allow-comment-only", "# just a comment", None),
]

PATH_CASES = [
    ("path-env", ".env", "secrets"),
    ("path-env-local", "/srv/ws/.env.local", "secrets"),
    ("path-env-prod", "/srv/ws/config/.env.production", "secrets"),
    ("path-env-double-suffix", ".env.production.local", "secrets"),
    ("path-netrc", "~/.netrc", "secrets"),
    ("path-pgpass", "$HOME/.pgpass", "secrets"),
    ("path-git-credentials", "${HOME}/.git-credentials", "secrets"),
    ("path-rsa", "id_rsa", "secrets"),
    ("path-dsa", "id_dsa", "secrets"),
    ("path-ecdsa", "id_ecdsa", "secrets"),
    ("path-ed25519", "id_ed25519", "secrets"),
    ("path-pem", "config/private.pem", "secrets"),
    ("path-p12", "config/private.p12", "secrets"),
    ("path-pfx", "config/private.pfx", "secrets"),
    ("path-credentials", "credentials.json", "secrets"),
    ("path-service-account", "service-account-demo.json", "secrets"),
    ("path-ssh-dir", "/home/demo/.ssh/config", "secrets"),
    ("path-gnupg-dir", "/home/demo/.gnupg/trustdb.gpg", "secrets"),
    ("path-secret-dir", "/srv/acc/data/secrets/demo", "secrets"),
    ("path-aws", "~/.aws/credentials", "secrets"),
    ("path-docker", "$HOME/.docker/config.json", "secrets"),
    ("path-gh", "${HOME}/.config/gh/hosts.yml", "secrets"),
    ("path-codex", "/home/demo/.codex/auth.json", "secrets"),
    ("path-claude", "/home/demo/.claude/.credentials.json", "secrets"),
    ("path-copilot", "/home/demo/.copilot/config.json", "secrets"),
    ("path-antigravity-plugin", "~/.config/antigravity/plugin-secrets.json", "secrets"),
    ("path-antigravity-oauth", "~/.gemini/antigravity-cli/antigravity-oauth-token", "secrets"),
    ("path-cursor", "~/.config/cursor/auth.json", "secrets"),
    ("path-kube", "~/.kube/config", "secrets"),
    ("path-relative-ssh", ".ssh/config", "secrets"),
    ("path-relative-gnupg", ".gnupg/trustdb.gpg", "secrets"),
    ("path-dot-component", "/home/demo/.aws/./credentials", "secrets"),
    ("path-example", ".env.example", None),
    ("path-sample", ".env.sample", None),
    ("path-template", ".env.template", None),
    ("path-dist", ".env.dist", None),
    ("path-defaults", ".env.defaults", None),
    ("path-public-rsa", "~/.ssh/id_rsa.pub", None),
    ("path-public-ed25519", "~/.ssh/id_ed25519.pub", None),
    ("path-source", "/srv/ws/src/app.py", None),
    ("path-home-notes", "~/notes.txt", None),
    ("path-aws-config", "~/.aws/config", None),
    ("path-session-env", "/srv/acc/run/agent/0123456789ab/session.env", "secrets"),
    ("path-docker-readme", "~/.docker/README.md", None),
    ("path-env-directory", "/srv/ws/.env/notes.txt", None),
    ("path-env-similar", "/srv/ws/.environment", None),
    ("path-secret-sibling", "/srv/acc/data/secrets-other/example", None),
    ("path-dir-normalize", "/home/demo/.ssh/../notes.txt", None),
    ("path-env-normalize", "/srv/ws/.env/../notes.txt", None),
    ("path-safe-nested", "/srv/ws/config/.env.example", None),
    ("path-not-shell-quoted", "'/srv/ws/.env'", None),
]

# Custom patterns match the raw command text (so quoted matches block by design, unlike built-in command rules) and
# each command's words as the shell reads them, quotes removed.
CUSTOM_CASES = [
    ("custom-destroy", r"\bterraform\s+destroy\b", "terraform destroy", "custom"),
    ("custom-flags", r"\bterraform\s+destroy\b", "terraform destroy -auto-approve", "custom"),
    ("custom-chain", r"\bterraform\s+destroy\b", "cd infra && terraform destroy", "custom"),
    ("custom-tabs", r"\bterraform\s+destroy\b", "terraform\tdestroy", "custom"),
    ("custom-newline", r"\bterraform\s+destroy\b", "terraform\ndestroy", "custom"),
    ("custom-quoted", r"\bterraform\s+destroy\b", "echo 'terraform destroy'", "custom"),
    ("custom-comment", r"\bterraform\s+destroy\b", "# terraform destroy", "custom"),
    ("custom-plan", r"\bterraform\s+destroy\b", "terraform plan", None),
    ("custom-word-boundary", r"\bterraform\s+destroy\b", "terraform destroyer", None),
    ("custom-case", r"\bterraform\s+destroy\b", "TERRAFORM DESTROY", None),
    ("custom-case-insensitive", r"(?i)\bterraform\s+destroy\b", "TERRAFORM DESTROY", "custom"),
    ("custom-invalid", "(", "terraform destroy", None),
    ("custom-anchor-match", r"^deploy prod$", "deploy prod", "custom"),
    ("custom-anchor-miss", r"^deploy prod$", "echo deploy prod", None),
    ("custom-before-builtin", r"git push", "git push -f origin main", "custom"),
    ("custom-literal-path", r"/opt/protected", "echo /opt/protected", "custom"),
    ("custom-parsed-quotes", r"\bterraform\s+destroy\b", 'ter""raform destroy', "custom"),
    ("custom-parsed-single", r"\bterraform\s+destroy\b", "terraform 'destroy'", "custom"),
    ("custom-parsed-escape", r"\bterraform\s+destroy\b", "terr\\aform destroy", "custom"),
    ("custom-parsed-shell", r"\bterraform\s+destroy\b", "sh -c 'ter\"\"raform destroy'", "custom"),
    ("custom-parsed-plan", r"\bterraform\s+destroy\b", 'ter""raform plan', None),
]

# The session's own guardrails and the CLIs' hook settings (X6/D2, D16): read freely, never changed through a tool call.
RUN = "/srv/acc/run/agent/0123456789ab"
OWN_FILE_CASES = [
    ("own-redirect", "printf '{}' > \"$ACC_RUN_DIR/guardrails.json\"", "own_files"),
    ("own-redirect-braces", "echo '{}' >${ACC_RUN_DIR}/guardrails.json", "own_files"),
    ("own-redirect-literal", f"echo x >> {RUN}/settings.json", "own_files"),
    ("own-redirect-clobber", "echo x >| $ACC_RUN_DIR/settings.json", "own_files"),
    ("own-tee", "echo '{}' | tee $ACC_RUN_DIR/guardrails.json", "own_files"),
    ("own-rm", "rm -f $ACC_RUN_DIR/guardrails.json", "own_files"),
    ("own-rm-glob", "rm $ACC_RUN_DIR/*.json", "own_files"),
    ("own-mv", "mv $ACC_RUN_DIR/settings.json /tmp/x", "own_files"),
    ("own-mv-folder", "mv $ACC_RUN_DIR /tmp/x", "own_files"),
    ("own-mv-parent", "mv /srv/acc/run/agent /tmp/x", "own_files"),
    ("own-cp-over", f"cp /tmp/x {RUN}/guardrails.json", "own_files"),
    ("own-chmod", "chmod u+w $ACC_RUN_DIR/guardrails.json", "own_files"),
    ("own-sed", "sed -i s/true/false/ $ACC_RUN_DIR/guardrails.json", "own_files"),
    ("own-cd", "cd $ACC_RUN_DIR && sed -i s/true/false/ guardrails.json", "own_files"),
    ("own-truncate", "truncate -s0 $ACC_RUN_DIR/guardrails.json", "own_files"),
    ("own-spool", "echo '{}' >> $ACC_RUN_DIR/events.jsonl", "own_files"),
    ("own-python", "python3 -c 1 > $ACC_RUN_DIR/guardrails.json", "own_files"),
    ("own-shell", "sh -c 'rm $ACC_RUN_DIR/settings.json'", "own_files"),
    ("own-claude-local", "echo '{\"disableAllHooks\": true}' > .claude/settings.local.json", "own_files"),
    ("own-claude-user", "cp /tmp/x ~/.claude/settings.json", "own_files"),
    ("own-claude-glob", "rm .claude/settings*", "own_files"),
    ("own-codex-hooks", "rm ~/.codex/hooks.json", "own_files"),
    ("own-codex-config", "sed -i /hooks/d ~/.codex/config.toml", "own_files"),
    ("own-cursor-hooks", "echo '{}' > /home/demo/.cursor/hooks.json", "own_files"),
    ("own-copilot-hooks", "rm -r ~/.copilot/hooks", "own_files"),
    ("own-repo-copilot-hooks", "echo '{}' > .github/hooks/acc.json", "own_files"),
    # In a trusted folder these switch every user-level Copilot hook off (1.0.102, qualification attempt 16).
    ("own-repo-copilot-settings", "echo '{\"disableAllHooks\": true}' > .github/copilot/settings.json", "own_files"),
    ("own-repo-copilot-local", "cp /tmp/x .github/copilot/settings.local.json", "own_files"),
    ("own-antigravity-hooks", "mv ~/.gemini/config/hooks.json /tmp", "own_files"),
    ("allow-own-cat", "cat $ACC_RUN_DIR/guardrails.json", None),
    ("allow-own-jq", "jq .rules $ACC_RUN_DIR/guardrails.json", None),
    ("allow-own-ls", "ls -la $ACC_RUN_DIR", None),
    ("allow-own-grep", "grep -n PreToolUse ~/.claude/settings.json", None),
    ("allow-own-diff", "diff .claude/settings.json /tmp/x", None),
    ("allow-own-sibling", "echo x > /srv/acc/run/agent/other-notes.txt", None),
    ("allow-own-echo", "echo $ACC_RUN_DIR/guardrails.json", None),
    ("allow-own-fd", "make 2>&1 | tee build.log", None),
    ("allow-own-workspace", "echo '{}' > config/settings.json", None),
]

# Edit tools: other settings in a hook file may change, hooks may not; nothing in the run folder may change.
OWN_TOOL_CASES = [
    ("tool-write-policy", "Write", {"file_path": f"{RUN}/guardrails.json", "content": "{}"}, "own_files"),
    ("tool-edit-settings", "Edit", {"file_path": f"{RUN}/settings.json", "old_string": "a", "new_string": "b"}, "own_files"),
    ("tool-disable-hooks", "Edit", {"file_path": "/srv/ws/.claude/settings.local.json", "old_string": "{",
                                    "new_string": '{"disableAllHooks": true,'}, "own_files"),
    ("tool-remove-hooks", "Edit", {"file_path": "/home/demo/.claude/settings.json", "old_string": '"hooks": {}',
                                   "new_string": ""}, "own_files"),
    ("tool-codex-patch", "apply_patch", {"input": "*** Begin Patch\n*** Update File: /home/demo/.codex/hooks.json\n"
                                                  "-  \"hooks\": {}\n*** End Patch"}, "own_files"),
    ("tool-antigravity-write", "write_to_file", {"TargetFile": "/home/demo/.gemini/config/hooks.json",
                                                 "CodeContent": "{}"}, "own_files"),
    ("tool-copilot-repo-settings", "Write", {"file_path": "/srv/ws/.github/copilot/settings.json",
                                             "content": '{"disableAllHooks": true}'}, "own_files"),
    ("tool-copilot-repo-model", "Write", {"file_path": "/srv/ws/.github/copilot/settings.local.json",
                                          "content": '{"model": "gpt-4o"}'}, None),
    ("tool-allow-permissions", "Edit", {"file_path": "/srv/ws/.claude/settings.json", "old_string": '"allow": [',
                                        "new_string": '"allow": ["Bash(npm test)",'}, None),
    ("tool-allow-read", "Read", {"file_path": f"{RUN}/guardrails.json"}, None),
    ("tool-allow-view", "view_file", {"AbsolutePath": f"{RUN}/settings.json"}, None),
]

# D15: the secrets rule covers search tools' filters and folders, credential folders, the run folder's MCP files,
# globs that could name a secret, and commands that print tokens or the whole environment.
SECRET_TOOL_CASES = [
    ("tool-grep-glob", "Grep", {"pattern": "key", "glob": "**/.aws/credentials", "output_mode": "content"}, "secrets"),
    ("tool-grep-path", "Grep", {"pattern": "key", "path": "/home/demo/.aws"}, "secrets"),
    ("tool-search-path", "grep_search", {"Query": "key", "SearchPath": "/home/demo/.ssh/id_ed25519"}, "secrets"),
    ("tool-read-mcp", "Read", {"file_path": f"{RUN}/mcp.json"}, "secrets"),
    ("tool-grep-env-glob", "Grep", {"pattern": "KEY", "glob": ".env*"}, "secrets"),
    ("tool-allow-grep-json", "Grep", {"pattern": "name", "glob": "*.json"}, None),
    ("tool-allow-grep-src", "Grep", {"pattern": "key", "path": "/srv/ws/src"}, None),
    ("tool-allow-read-prompt", "Read", {"file_path": f"{RUN}/append-prompt.md"}, None),
]
SECRET_COMMAND_CASES = [
    ("secret-glob-env", "cat .en?", "secrets"),
    ("secret-glob-auth", "cat ~/.codex/auth.jso[n]", "secrets"),
    ("secret-glob-key", "cat ~/.ssh/id_*", "secrets"),
    ("secret-aws-folder", "grep -r . ~/.aws", "secrets"),
    ("secret-gh-folder", "tar czf /tmp/x.tgz ~/.config/gh", "secrets"),
    ("secret-run-mcp", 'cat "$ACC_RUN_DIR/mcp.json"', "secrets"),
    ("secret-run-mcp-literal", f"jq . {RUN}/mcp.json", "secrets"),
    ("secret-gh-token", "gh auth token", "secrets"),
    ("secret-gh-show-token", "gh auth status --show-token", "secrets"),
    ("secret-printenv", "printenv", "secrets"),
    ("secret-env-dump", "env", "secrets"),
    ("secret-proc-environ", "cat /proc/1234/environ", "secrets"),
    ("secret-proc-self", "tr '\\0' '\\n' < /proc/self/environ", "secrets"),
    ("allow-secret-printenv-name", "printenv PATH", None),
    ("allow-secret-env-command", "env FOO=1 make test", None),
    ("allow-secret-gh-status", "gh auth status", None),
    ("allow-secret-star", "cat *.md", None),
    ("allow-secret-json-glob", "cat *.json", None),
    ("allow-secret-dot-glob", "ls -d .*", None),
    ("allow-secret-aws-config", "cat ~/.aws/config", None),
    ("allow-secret-run-prompt", "cat $ACC_RUN_DIR/append-prompt.md", None),
]

# Merging pull requests is a person's action (docs/PR-FINISHING.md). Aliases come from GH_ALIASES (gh's config.yml).
GH_ALIASES = {"mm": "pr merge --squash", "land": "!gh pr merge --admin \"$1\"", "pv": "pr view", "sh-ok": "!gh pr checks"}
MERGE_CASES = [
    ("merge-plain", "gh pr merge 12", "merge_pr"),
    ("merge-squash-auto", "gh pr merge --auto --squash 12", "merge_pr"),
    ("merge-current-branch", "gh pr merge", "merge_pr"),
    ("merge-repo-before", "gh -R example-org/demo pr merge 12", "merge_pr"),
    ("merge-repo-between", "gh pr --repo example-org/demo merge 12", "merge_pr"),
    ("merge-repo-equals", "gh pr merge --repo=example-org/demo 12 --rebase", "merge_pr"),
    ("merge-repo-joined", "gh pr -Rexample-org/demo merge 12", "merge_pr"),
    ("merge-absolute", "/usr/bin/gh pr merge 12", "merge_pr"),
    ("merge-quoted", "gh 'pr' \"merge\" 12", "merge_pr"),
    ("merge-split-quote", "gh p''r mer'ge' 12", "merge_pr"),
    ("merge-sudo-env", "sudo -u builder env GH_REPO=x gh pr merge 12", "merge_pr"),
    ("merge-timeout", "timeout 30 gh pr merge 12", "merge_pr"),
    ("merge-xargs", "echo 12 | xargs gh pr merge", "merge_pr"),
    ("merge-and", "gh pr checks 12 && gh pr merge 12", "merge_pr"),
    ("merge-shell", "bash -c 'gh pr merge 12 --squash'", "merge_pr"),
    ("merge-eval", "eval 'gh pr merge 12'", "merge_pr"),
    ("merge-substitution", "echo $(gh pr merge 12)", "merge_pr"),
    ("merge-variable-command", "GH=gh; $GH pr merge 12", "merge_pr"),
    ("merge-unknown-command", "$GH pr merge 12", "merge_pr"),
    ("merge-variable-action", "A=merge; gh pr $A 12", "merge_pr"),
    ("merge-unknown-action", "gh pr $ACTION 12", "merge_pr"),
    ("merge-unknown-sub", "gh $SUB merge 12", "merge_pr"),
    ("merge-find", r"find . -name x -exec gh pr merge 12 \;", "merge_pr"),
    ("merge-alias", "gh mm 12", "merge_pr"),
    ("merge-alias-shell", "gh land 12", "merge_pr"),
    ("merge-alias-set", "gh alias set m2 'pr merge --squash'", "merge_pr"),
    ("merge-alias-set-shell", "gh alias set --shell m3 'gh pr merge \"$1\"'", "merge_pr"),
    ("merge-alias-set-bang", "gh alias set m4 '!gh pr merge $1'", "merge_pr"),
    ("merge-alias-set-stdin", "gh alias set m5 - <<'EOF'\npr merge --auto\nEOF", "merge_pr"),
    ("merge-alias-import", "gh alias import aliases.yml", "merge_pr"),
    ("merge-api-put", "gh api -X PUT repos/example-org/demo/pulls/12/merge", "merge_pr"),
    ("merge-api-method-long", "gh api --method=PUT /repos/example-org/demo/pulls/12/merge -f merge_method=squash", "merge_pr"),
    ("merge-api-joined", "gh api -XPUT repos/{owner}/{repo}/pulls/12/merge", "merge_pr"),
    ("merge-api-field-implies-post", "gh api repos/o/r/pulls/12/merge -f merge_method=squash", "merge_pr"),
    ("merge-api-url", "gh api -X PUT https://api.github.com/repos/o/r/pulls/12/merge", "merge_pr"),
    ("merge-api-repo-flag", "gh -R o/r api --method PUT repos/o/r/pulls/12/merge", "merge_pr"),
    ("merge-api-variables", 'gh api -X PUT "repos/$OWNER/$REPO/pulls/$N/merge"', "merge_pr"),
    ("merge-graphql", "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"PR_1\"}) { clientMutationId } }'", "merge_pr"),
    ("merge-graphql-auto", "gh api graphql -F query='mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }' -F id=PR_1", "merge_pr"),
    ("merge-graphql-joined", "gh api graphql -fquery='mutation{mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}'", "merge_pr"),
    ("merge-graphql-stdin", "gh api graphql --input - <<'EOF'\n{\"query\": \"mutation { mergePullRequest(input: {pullRequestId: \\\"x\\\"}) { clientMutationId } }\"}\nEOF", "merge_pr"),
    ("merge-graphql-unseen-stdin", "cat q.json | gh api graphql --input -", "merge_pr"),
    ("merge-graphql-unreadable-file", "gh api graphql -F query=@/nonexistent/merge.graphql", "merge_pr"),
    ("merge-graphql-unknown-query", 'gh api graphql -f query="$Q"', "merge_pr"),
    ("allow-pr-view", "gh pr view 12", None),
    ("allow-pr-view-repo", "gh pr view 12 -R example-org/demo --json mergeable,mergeStateStatus", None),
    ("allow-pr-checks", "gh pr checks 12 --watch", None),
    ("allow-pr-create", "gh pr create --fill --base main --title 'Merge the login form'", None),
    ("allow-pr-create-draft", "gh pr create --draft --body 'Do not merge yet: gh pr merge comes later'", None),
    ("allow-pr-ready", "gh pr ready 12", None),
    ("allow-pr-review", "gh pr review 12 --comment -b 'merge conflicts fixed'", None),
    ("allow-pr-list", "gh pr list --search 'is:merged'", None),
    ("allow-pr-status", "gh pr status", None),
    ("allow-pr-number-variable", 'gh pr view "$N"', None),
    ("allow-alias-view", "gh pv 12", None),
    ("allow-alias-shell", "gh sh-ok", None),
    ("allow-alias-set", "gh alias set co2 'pr checkout'", None),
    ("allow-alias-list", "gh alias list", None),
    ("allow-unknown-extension", "gh dash", None),
    ("allow-api-merge-status", "gh api repos/o/r/pulls/12/merge", None),
    ("allow-api-pulls", "gh api repos/o/r/pulls/12", None),
    ("allow-api-merges", "gh api -X POST repos/o/r/merges -f base=main -f head=topic", None),
    ("allow-api-graphql-read", "gh api graphql -f query='{ repository(owner: \"o\", name: \"r\") { pullRequest(number: 1) { mergeable } } }'", None),
    ("allow-api-graphql-vars", 'gh api graphql -f query=\'query($n: Int!) { viewer { login } }\' -F n="$N"', None),
    ("allow-api-variables", 'gh api "repos/$OWNER/$REPO/pulls/$N/comments"', None),
    ("allow-issue-variable", 'gh issue view "$N" --comments', None),
    ("allow-echo", "echo 'gh pr merge 12'", None),
    ("allow-grep", "grep -r 'gh pr merge' docs", None),
    ("allow-git-merge", "git merge --no-ff topic", None),
    ("allow-other-dynamic", "$CC $CFLAGS main.c", None),
]

DISABLED_CASES = [
    ("off-merge", "merge_pr", "gh pr merge 12", None),
    ("off-merge-api", "merge_pr", "gh api -X PUT repos/o/r/pulls/12/merge", None),
    ("off-merge-still-default", "merge_pr", "git push origin main", "push_default"),
    ("d14-off-alias-force", "force_push", "git -c alias.p=push p origin main", "push_default"),
    ("d14-off-force-prefix", "force_push", "git -C feature push --force-w origin topic", None),
    ("d14-off-subtree-default", "push_default", "git subtree push -P lib origin main", None),
    ("d14-off-dynamic-rm", "rm_outside", "$RUN -rf /opt/example", None),
    ("d14-off-runner-secrets", "secrets", "script -qc 'cat .env' /dev/null", None),
    ("off-force", "force_push", "git push -f origin topic", None),
    ("off-force-still-default", "force_push", "git push -f origin main", "push_default"),
    ("off-default", "push_default", "git push origin main", None),
    ("off-default-still-force", "push_default", "git push -f origin main", "force_push"),
    ("off-rm", "rm_outside", "rm -rf /opt/example", None),
    ("off-secrets", "secrets", "cat .env", None),
    ("off-secrets-still-default", "secrets", "git push origin main", "push_default"),
    ("off-rm-still-secrets", "rm_outside", "cat .env", "secrets"),
]


@pytest.fixture(autouse=True)
def string_only(monkeypatch):
    def no_process(*args, **kwargs):
        raise AssertionError("Corpus input must never launch a subprocess")

    def branch(directory):
        return BRANCHES.get(directory, "feature/topic")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(guard, "current_branch", branch)
    monkeypatch.setattr(guard, "gh_aliases", lambda: dict(GH_ALIASES))


@pytest.mark.parametrize("case_id,command,expected", COMMAND_CASES, ids=[row[0] for row in COMMAND_CASES])
def test_command_corpus(case_id, command, expected):
    verdict = guard.check(POLICY, "Bash", {"command": command}, "/srv/ws")
    assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("case_id,command,expected", MERGE_CASES, ids=[row[0] for row in MERGE_CASES])
def test_merge_corpus(case_id, command, expected):
    verdict = guard.check(POLICY, "Bash", {"command": command}, "/srv/ws")
    assert (verdict[0] if verdict else None) == expected, case_id
    if expected:
        assert "a person merges" in verdict[1] or "can't" in verdict[1]


@pytest.mark.parametrize("case_id,path,expected", PATH_CASES, ids=[row[0] for row in PATH_CASES])
def test_path_corpus(case_id, path, expected):
    verdict = guard.check(POLICY, "Read", {"file_path": path}, "/srv/ws")
    assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("case_id,pattern,command,expected", CUSTOM_CASES, ids=[row[0] for row in CUSTOM_CASES])
def test_custom_corpus(case_id, pattern, command, expected):
    policy = {**POLICY, "custom": [{"pattern": pattern, "reason": "corpus custom rule"}]}
    verdict = guard.check(policy, "Bash", {"command": command}, "/srv/ws")
    assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("case_id,disabled,command,expected", DISABLED_CASES, ids=[row[0] for row in DISABLED_CASES])
def test_disabled_corpus(case_id, disabled, command, expected):
    policy = {**POLICY, "rules": {**POLICY["rules"], disabled: False}}
    verdict = guard.check(policy, "Bash", {"command": command}, "/srv/ws")
    assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("case_id,command,expected", OWN_FILE_CASES, ids=[row[0] for row in OWN_FILE_CASES])
def test_own_files_corpus(case_id, command, expected):
    off = {**POLICY, "rules": {}}  # protected whatever the rules
    for policy in (POLICY, off):
        verdict = guard.check(policy, "Bash", {"command": command}, "/srv/ws", RUN)
        assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("case_id,tool,args,expected", OWN_TOOL_CASES, ids=[row[0] for row in OWN_TOOL_CASES])
def test_own_files_tool_corpus(case_id, tool, args, expected):
    verdict = guard.check(POLICY, tool, args, "/srv/ws", RUN)
    assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("case_id,tool,args,expected", SECRET_TOOL_CASES, ids=[row[0] for row in SECRET_TOOL_CASES])
def test_secret_tool_corpus(case_id, tool, args, expected):
    verdict = guard.check(POLICY, tool, args, "/srv/ws", RUN)
    assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("case_id,command,expected", SECRET_COMMAND_CASES, ids=[row[0] for row in SECRET_COMMAND_CASES])
def test_secret_command_corpus(case_id, command, expected):
    verdict = guard.check(POLICY, "Bash", {"command": command}, "/srv/ws", RUN)
    assert (verdict[0] if verdict else None) == expected, case_id


@pytest.mark.parametrize("key", ["Cwd", "workdir", "cwd"])
@pytest.mark.parametrize("command,directory,expected", [
    ("rm -rf build", "/opt/other", "rm_outside"),
    ("rm -rf build", "sub", None),
    ("git push origin HEAD", "/srv/ws/main", "push_default"),
    ("git push origin HEAD", "/srv/ws/feature", None),
    ("cat ../.ssh/config", "/home/example/work", "secrets"),
])
def test_tool_cwd_corpus(key, command, directory, expected):
    verdict = guard.check(POLICY, "Bash", {"command": command, key: directory}, "/srv/ws")
    assert (verdict[0] if verdict else None) == expected


PRIVATE_CASES = [
    ('dynamic-git-action', 'git "$ACTION" origin HEAD', 'local_only'),
    ('dynamic-gh-action', 'gh repo "$ACTION" demo', 'local_only'),
    ('private-alias', "git -c alias.publish=push publish origin feature", 'local_only'),
    ('remote-global-option', 'git remote --verbose add origin /tmp/demo.git', 'local_only'),
    ('nested-push', "printf '%s' $(git push /tmp/demo.git HEAD)", 'local_only'),
    ('push-branch', 'git push origin feature', 'local_only'),
    ('push-url', 'git -C feature push https://example.com/demo.git HEAD', 'local_only'),
    ('remote-add', 'git remote add origin https://example.com/demo.git', 'local_only'),
    ('remote-set', 'git remote set-url --push origin https://example.com/demo.git', 'local_only'),
    ('nested', "env sh -c 'git remote add backup /tmp/demo.git'", 'local_only'),
    ('create', 'gh repo create demo --private', 'local_only'),
    ('wrapped-create', 'command gh --hostname example.com repo create demo', 'local_only'),
    ('subtree', 'git subtree push --prefix lib origin feature', 'local_only'),
    ('status', 'git status', None),
    ('remote-read', 'git remote -v', None),
    ('printed', "printf '%s' 'git push origin feature'", None),
    ('local-commit', 'git commit -m work', None),
]


@pytest.mark.parametrize('label,source,want', PRIVATE_CASES, ids=[c[0] for c in PRIVATE_CASES])
def test_private_workspace_corpus(label, source, want):
    policy = {**POLICY, 'local_only': True, 'rules': {}}
    verdict = guard.check(policy, 'Bash', {'command': source}, '/srv/ws')
    assert (verdict[0] if verdict else None) == want


@pytest.mark.parametrize('source', ['git push origin HEAD:feature', 'git remote add origin https://example.com/demo.git',
                                  'git remote set-url origin https://example.com/demo.git', 'gh repo create demo --private'])
def test_project_remote_operations_remain_available(source):
    assert guard.check({**POLICY, 'rules': {}}, 'Bash', {'command': source}, '/srv/ws') is None


POWERSHELL = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
TOOLCHAIN_PROBES = (
    'rustup toolchain list; rustup +stable-x86_64-pc-windows-msvc target list --installed; '
    'cargo +stable-x86_64-pc-windows-msvc --version; rustc +stable-x86_64-pc-windows-msvc --version'
)
VS_PROBE = (
    r'$vs = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" '
    '-products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 '
    '-property installationPath; "VS: $vs"'
)


@pytest.mark.parametrize('script', [TOOLCHAIN_PROBES, TOOLCHAIN_PROBES + '; ' + VS_PROBE])
def test_dynamic_powershell_reports_uncertain_command_not_recursive_delete(script):
    source = f"cd /mnt/c && PS={POWERSHELL}; $PS -NoProfile -Command '{script}' 2>&1 | tail -20"
    result = guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws')
    assert result is not None
    assert 'command name comes from a variable' in result[1]
    assert 'recursive delete' not in result[1]


@pytest.mark.parametrize('script', ['rustup toolchain list', TOOLCHAIN_PROBES])
def test_literal_powershell_toolchain_queries(script):
    source = f"{POWERSHELL} -NoProfile -Command '{script}'"
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is None


@pytest.mark.parametrize('executable', [POWERSHELL, 'powershell', 'pwsh', 'pwsh.exe'])
@pytest.mark.parametrize('script', [
    'Remove-Item -Recurse /opt/example',
    'Remove-Item -Recurse C:\\Users\\Example',
    'rm -r /opt/example',
    'rustup toolchain list; Remove-Item -Recurse /opt/example',
    '& $cmd -Recurse /opt/example',
])
def test_powershell_delete_or_unknown_script_stays_refused(executable, script):
    result = guard.check(POLICY, 'Bash', {'command': f"{executable} -NoProfile -Command '{script}'"}, '/srv/ws')
    assert result is not None


@pytest.mark.parametrize('arguments', [
    '-EncodedCommand ZQBjAGgAbwA=', '-File /opt/example.ps1',
    '-Command "rustup toolchain list" -Command "Remove-Item -Recurse /opt/example"',
    '-Command "$x"', '-Command "rustup toolchain list" extra',
])
def test_unreadable_powershell_execution_fails_closed(arguments):
    assert guard.check(POLICY, 'Bash', {'command': f'{POWERSHELL} {arguments}'}, '/srv/ws') is not None


@pytest.mark.parametrize('script', ['rustup toolchain\nlist', 'cargo\n--version', 'rustup\rtoolchain list'])
def test_powershell_query_words_cannot_hide_statement_boundaries(script):
    source = f"{POWERSHELL} -NoProfile -Command '{script}'"
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is not None


# Security review of guard-dynamic-command-reason (docs/security/2026-10-25-review-guard-dynamic-command-reason.md):
# refusals that must hold however the program is named or nested.
@pytest.mark.parametrize('source', [
    'rm -Rf /opt/example', 'rm -fR /opt/example', 'rm -dfr /opt/example', 'rm -vfr /opt/example',
    'rm --recursive --force /opt/example', 'rm --rec /opt/example', 'rm -f -r -- /opt/example',
    r'\rm -rf /opt/example', "'rm' -rf /opt/example", '/bin/rm -rf /opt/example', 'command rm -rf /opt/example',
    'env -i rm -rf /opt/example', 'nice -n 5 rm -rf /opt/example', 'sudo rm -rf /opt/example', 'exec rm -rf /opt/example',
    'X=rm; $X -rf /opt/example', '$X -rf /opt/example', '"$X" -fr /opt/example', '$(echo rm) -rf /opt/example',
    '$RM -NoProfile /opt/example', 'eval "rm -rf /opt/example"', 'X=rm; eval "$X -rf /opt/example"',
    "bash -c \"bash -c 'rm -rf /opt/example'\"", "sh -c 'X=rm; $X -rf /opt/example'", "xargs bash -c 'rm -rf /opt/example'",
    'f() { rm -rf "$1"; }; f /opt/example', 'D=/opt; rm -rf "$D/example"', 'cd /opt; rm -rf example',
    'rm -rf /mnt/c/Users/Example', 'rm -rf /mnt/c', 'rm -rf /mnt/C/Users/Example', 'cd /mnt/c && rm -rf Users',
])
def test_recursive_delete_refusals_hold_across_spellings_and_nesting(source):
    result = guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws')
    assert result is not None and result[0] == 'rm_outside', (source, result)


@pytest.mark.parametrize('source', [
    "PS=powershell.exe; $PS -Command 'Remove-Item -Recurse C:\\x'",
    f"PS={POWERSHELL}; $PS -NoProfile -Command 'Remove-Item -Recurse /opt/example'",
    "$PS -NoProfile -EncodedCommand ZQBjAGgAbwA=", "$PS -ExecutionPolicy Bypass -File /opt/x.ps1",
    "xargs powershell -Command 'Remove-Item -Recurse C:\\x'", "timeout 5 pwsh -c 'Remove-Item -Recurse /opt/x'",
    "find . -exec powershell -Command 'Remove-Item -Recurse C:\\x' \;", "bash -lc \"pwsh -c 'Remove-Item -Recurse /opt/x'\"",
    "pwsh-preview -c 'Remove-Item -Recurse /opt/x'", "powershell_ise.exe -Command 'Remove-Item -Recurse C:\\x'",
    "echo 'Remove-Item -Recurse C:\\x' | powershell -Command -", "powershell -enc ZQBjAGgAbwA=",
])
def test_powershell_named_by_a_variable_or_wrapper_stays_refused(source):
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is not None, source


def test_powershell_from_a_variable_says_so_and_keeps_toolchain_queries():
    refused = guard.check(POLICY, 'Bash', {'command': "PS=pwsh; $PS -Command 'Remove-Item -Recurse /opt/x'"}, '/srv/ws')
    assert refused and 'command name comes from a variable' in refused[1] and 'recursive delete' not in refused[1]
    for source in (f"PS={POWERSHELL}; $PS -NoProfile -Command 'rustup toolchain list'",
                   "$PS -NoProfile -Command 'cargo --version'"):
        assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is None, source


@pytest.mark.parametrize('source, rule', [
    ("$SHELL -c 'rm -rf /opt/example'", 'rm_outside'),
    ("SH=bash; $SH -c 'git push -f origin main'", 'force_push'),
    ("$SHELL -lc 'cat .env'", 'secrets'),
])
def test_unknown_program_with_a_c_script_is_checked_as_a_shell(source, rule):
    result = guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws')
    assert result is not None and result[0] == rule, (source, result)


@pytest.mark.parametrize('source', ["$CC -c foo.c -o foo.o", "$PY -c 'print(1)'", "$SHELL -c 'echo hi'"])
def test_unknown_program_c_scripts_without_protected_operations_stay_allowed(source):
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is None


@pytest.mark.parametrize('source', [
    r'cmd.exe /c rmdir /s /q C:\Users\Example', r'cmd.exe /c "rd /s /q C:\Users\Example"',
    r'cmd.exe /c del /s /q C:\Users\Example', r'/mnt/c/Windows/System32/cmd.exe /c rd /s /q C:\Users\Example',
    r'cmd /k rd /s /q C:\x', r'cmd.exe /c rd/s/q C:\x', r'cmd.exe /C "R^D /S /Q C:\x"', r'cmd.exe /c "r""d /s /q C:\x"',
    r'cmd.exe /v:on /c "set X=rd&& !X! /s /q C:\x"', r'cmd.exe /c "%X:~0,2% /s /q C:\x"',
    r'cmd.exe /c robocopy C:\empty C:\Users\Example /mir', 'cmd.exe /c powershell -c x', 'cmd.exe /c git push -f',
    r'echo rd /s /q C:\x | cmd.exe', r'C=cmd.exe; $C /c rd /s /q C:\x', r'cmd.exe /c $SCRIPT',
    r'sh -c "cmd.exe /c rd /s /q C:\\Users\\Example"', 'cmd.exe /x /c echo hi',
])
def test_windows_cmd_deletes_and_hidden_commands_are_refused(source):
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is not None, source


@pytest.mark.parametrize('source', [
    'cmd.exe /c echo %USERPROFILE%', 'cmd.exe /c ver', 'cmd.exe /d /c echo hi',
    'wslpath "$(cmd.exe /c echo %USERPROFILE% 2>/dev/null)"',
])
def test_windows_cmd_queries_stay_allowed(source):
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is None


@pytest.mark.parametrize('source', [
    'find /opt/example -delete', 'find /opt -name x -delete', 'find / -delete', 'find -L /opt -delete',
    'find $D -delete', 'find -files0-from list -delete', 'find /mnt/c/Users -delete',
    'find /opt -exec rm -rf {} +', r'find /opt -exec rm -rf {} \;', 'find / -name node_modules -exec rm -rf {} +',
    'find /mnt/c/Users/Example -exec rm -r {} +', 'find $D -exec rm -rf {} +',
])
def test_find_deletes_outside_the_workspace_are_refused(source):
    result = guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws')
    assert result is not None and result[0] == 'rm_outside', (source, result)


@pytest.mark.parametrize('source', [
    "find . -name '*.pyc' -delete", "find /srv/ws -name '*.pyc' -delete", 'find build -delete',
    'find . -name node_modules -exec rm -rf {} +', "find . -name node_modules -prune -exec rm -rf '{}' +",
    r'find /opt -exec echo {} \;',
])
def test_find_inside_the_workspace_stays_allowed(source):
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is None


# Owner decisions on the review (GR-6, GR-7, GR-8), all tightening.
@pytest.mark.parametrize('flags', ['', '-NonInteractive', '-NoLogo', '-NonInteractive -NoLogo'])
def test_powershell_toolchain_queries_need_noprofile(flags):
    """Without -NoProfile, PowerShell runs the user's profile script before the query (GR-8)."""
    source = f"{POWERSHELL} {flags} -Command 'rustup toolchain list'".replace('  ', ' ')
    result = guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws')
    assert result is not None and '-NoProfile' in result[1]
    assert guard.check(POLICY, 'Bash', {'command': f"{POWERSHELL} {flags} -NoProfile -Command 'cargo --version'"},
                       '/srv/ws') is None


@pytest.mark.parametrize('source', [
    'find /opt | xargs rm -rf', 'find . -print0 | xargs -0 rm -rf', 'echo build | xargs rm -rf', 'xargs rm -r < list',
    'xargs -I{} rm -rf {} < list', 'xargs -a list rm -rf', 'sudo xargs rm -rf < list', 'ls | xargs -n1 rm --recursive',
    'xargs $RM -rf < list', "bash -c 'xargs rm -rf < list'",
])
def test_xargs_recursive_rm_is_refused_as_unverifiable(source):
    """xargs supplies rm's paths from its input, which the guard can't see (GR-6)."""
    result = guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws')
    assert result is not None and result[0] == 'rm_outside' and 'xargs' in result[1], (source, result)


@pytest.mark.parametrize('source', ['xargs rm -f < list', 'xargs echo rm -rf < list', 'xargs grep -l TODO < list'])
def test_xargs_without_recursive_rm_stays_allowed(source):
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is None


@pytest.mark.parametrize('source, rule', [
    ("alias r='rm -rf'\nr /opt/example", 'rm_outside'),
    ("alias r='rm -rf'; r /opt/example", 'rm_outside'),
    ("shopt -s expand_aliases\nalias r='rm -rf'\nr /opt/example", 'rm_outside'),
    ("bash -c $'shopt -s expand_aliases\\nalias r=\"rm -rf\"\\nr /opt/example'", 'rm_outside'),
    ("alias a=b b='rm -rf'\na /opt/example", 'rm_outside'),
    ("alias p='git push -f'\np origin topic", 'force_push'),
    ("alias c=cat\nc .env", 'secrets'),
    ("alias x='cd /opt; rm -rf'\nx example", 'rm_outside'),
    ("alias s='sudo '\ns rm -rf /opt/example", 'rm_outside'),
    ('alias r="$CMD"\nr /opt/example', 'rm_outside'),
])
def test_aliases_defined_in_the_command_are_followed(source, rule):
    """Claude Code runs commands with expand_aliases on, so a definition applies to the following lines (GR-7)."""
    result = guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws')
    assert result is not None and result[0] == rule, (source, result)


@pytest.mark.parametrize('source', [
    "alias r='rm -rf'\nr build", "alias ll='ls -l'\nll /opt", "alias r='rm -rf'\n\\r /opt/example",
    "alias r='rm -rf'\nunalias r\nr /opt/example", "alias x='cd /opt; rm -rf'\necho done",
])
def test_aliases_that_resolve_to_allowed_commands_stay_allowed(source):
    assert guard.check(POLICY, 'Bash', {'command': source}, '/srv/ws') is None
