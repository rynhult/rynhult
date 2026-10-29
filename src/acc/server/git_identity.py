"""Repository-local Git authorship, without machine or account configuration changes."""
import logging

from sqlalchemy import select

from .broker_client import BrokerError
from .models import AuditLog, User

WARNING = ("The Git author identity couldn't be set automatically. Open the project page, "
           "set Git author name and email in Git author, and save before committing. "
           "If the machine is offline, reconnect it first. Existing author settings are preserved.")


async def ensure(supervisor, project, actor, *, checkout=None, host=None):
    """Best-effort setup: a saved project or usable session must not become a 500."""
    try:
        await _ensure(supervisor, project, actor, checkout=checkout, host=host)
    except BrokerError:
        # Broker errors can include private paths/config text; never expose them.
        logging.getLogger(__name__).warning(WARNING)
        with supervisor.db() as db:
            db.add(AuditLog(actor=actor, action='project.git_identity_failed', target=project.slug,
                            detail={'message': WARNING, 'scope': 'repository-local'}))
            db.commit()
        return WARNING
    return None


async def _ensure(supervisor, project, actor, *, checkout=None, host=None):
    if project.archived or project.import_mode != 'managed' or project.group_id is not None:
        return
    with supervisor.db() as db:
        person = db.scalar(select(User).where(User.username == project.owner)) if project.owner else None
        # The profile currently displays username; no personal email is stored or needed.
        name = person.username if person else 'Rynhult'
        email = f'{person.username if person else "rynhult"}@noreply.rynhult.invalid'
    result = await supervisor.broker.call('git_identity', account=project.account, host=project.host or 'local',
                                          path=project.workspace_path, name=name, email=email)
    if result['changed']:
        with supervisor.db() as db:
            db.add(AuditLog(actor=actor, action='project.git_identity', target=project.slug,
                            detail={**result, 'source': 'profile_default', 'scope': 'repository-local'}))
            db.commit()

    # A helper on another machine receives a private Git repository, not a
    # linked worktree of the source repository. Git config does not travel in
    # bundles, so fill its missing identity from the project's effective author.
    if checkout and host and host != (project.host or 'local') and result['supported']:
        copied = await supervisor.broker.call('git_identity', account=project.account, host=host,
                                              path=checkout, name=result['name'], email=result['email'])
        if copied['changed']:
            with supervisor.db() as db:
                db.add(AuditLog(actor=actor, action='project.git_identity', target=project.slug,
                                detail={**copied, 'source': 'project_identity', 'scope': 'repository-local', 'host': host}))
                db.commit()
