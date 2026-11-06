"""P4 identities; additive sidecars, never filesystem permission or deletion custody."""
from .models import Base
from .source_schema_v1 import define

_tables = {table.name: table for table in define(Base.metadata)}


class HostingInstance(Base):
    __table__ = _tables['hosting_instance']


class SourceOwner(Base):
    __table__ = _tables['source_owner']


class Repository(Base):
    __table__ = _tables['repository']


class RepositoryLocator(Base):
    __table__ = _tables['repository_locator']


class SourceConnection(Base):
    __table__ = _tables['source_connection']


class ConnectionRepositoryAccess(Base):
    __table__ = _tables['connection_repository_access']


class ExecutionHost(Base):
    __table__ = _tables['source_execution_host']


class ExecutionAccount(Base):
    __table__ = _tables['source_execution_account']


class ProjectRepository(Base):
    __table__ = _tables['project_repository']


class WorkingLocation(Base):
    __table__ = _tables['working_location']


class LocationRegistration(Base):
    __table__ = _tables['location_registration']


class ProjectLocation(Base):
    __table__ = _tables['project_location']


class ProjectLaunchDefault(Base):
    __table__ = _tables['project_launch_default']


class SessionTarget(Base):
    __table__ = _tables['session_target']


# Register compatibility writers only after every mapped class exists (also when
# this module, rather than models, is imported first).
from . import sources as _sources  # noqa: E402,F401
