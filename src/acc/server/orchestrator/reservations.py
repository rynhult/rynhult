"""Orchestrator v2, slice 5: migration and decision number reservations (gap 9 of docs/ORCHESTRATOR-V2.md).

A reservation is a number, not a capability. reserve() hands out the next number that neither the team's base branch
(main) nor any open reservation uses; the gate later checks that a team branch adds only numbers that team reserved.
Numbers come from fixed layouts, never from agent input:
  migration  src/acc/server/migrations/versions/NNNN_*.py
  decision   docs/decisions/NNNN-*.md
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from ..models import AuditLog, Base, Session, UTCDateTime, utcnow
from ..supervisor_common import LaunchRefused

KINDS = ('migration', 'decision')
OPEN_LIMIT = 20                      # open reservations per team (design section 3)
LIFETIME = timedelta(days=14)        # an unused reservation lapses; the number becomes free again
LIVE = ('reserved', 'used')


class Reservation(Base):
    __tablename__ = 'reservation'
    __table_args__ = (UniqueConstraint('kind', 'number', name='uq_reservation_number'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    number: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[str | None] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey('session.id', ondelete='SET NULL'))
    branch: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(16), default='reserved')  # reserved | used | released
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


def lapse(db) -> None:
    """Expired reservations release their number (the row stays as the record)."""
    for row in db.scalars(select(Reservation).where(Reservation.state == 'reserved', Reservation.expires_at <= utcnow())):
        row.state = 'released'


def mark_used(db, numbers: dict[str, set[int]]) -> None:
    """A reserved number that now exists on the base branch has landed."""
    for kind, found in numbers.items():
        for row in db.scalars(select(Reservation).where(Reservation.kind == kind, Reservation.state == 'reserved',
                                                        Reservation.number.in_(sorted(found)))):
            row.state = 'used'


def reserve(db, kind: str, session: Session, base_numbers: dict[str, set[int]],
            pending: dict[str, set[int]] | None = None) -> Reservation:
    """The next number above everything taken: on the main line, on unmerged topic branches (`pending`), or reserved.
    Only numbers on the main line count as landed."""
    if kind not in KINDS:
        raise LaunchRefused('reserve takes migration or decision')
    lapse(db)
    mark_used(db, base_numbers)
    team_id = session.team_id
    open_rows = db.scalars(select(Reservation).where(Reservation.team_id == team_id, Reservation.state == 'reserved'))
    if len(list(open_rows)) >= OPEN_LIMIT:
        raise LaunchRefused(f'at most {OPEN_LIMIT} open reservations per team; release unused ones')
    taken = set(base_numbers.get(kind, set())) | set((pending or {}).get(kind, set())) | set(db.scalars(
        select(Reservation.number).where(Reservation.kind == kind, Reservation.state.in_(LIVE))))
    number = max(taken, default=0) + 1  # never a gap below the highest: numbers stay in landing order
    row = db.scalar(select(Reservation).where(Reservation.kind == kind, Reservation.number == number))
    if row is None:
        row = Reservation(kind=kind, number=number)
        db.add(row)
    row.team_id, row.session_id, row.branch, row.state = team_id, session.id, session.worktree_branch, 'reserved'
    row.created_at, row.expires_at = utcnow(), utcnow() + LIFETIME
    db.flush()
    db.add(AuditLog(actor=f'session:{session.id}', action='reservation.reserve', target=f'{kind}:{number:04d}',
                    detail={'team': team_id, 'branch': session.worktree_branch}))
    return row


def release(db, reservation_id: int, session: Session) -> Reservation:
    row = db.get(Reservation, reservation_id) if type(reservation_id) is int else None
    if row is None or row.team_id != session.team_id or row.state != 'reserved':
        raise LaunchRefused('no open reservation with that id in this team')
    row.state = 'released'
    db.add(AuditLog(actor=f'session:{session.id}', action='reservation.release', target=f'{row.kind}:{row.number:04d}',
                    detail={'team': row.team_id}))
    return row


def release_team(db, team_id, actor):
    # Landed (`used`) numbers stay used; unmerged branch scans still prevent reuse.
    for row in db.scalars(select(Reservation).where(Reservation.team_id == team_id, Reservation.state == 'reserved')):
        row.state = 'released'
        db.add(AuditLog(actor=actor, action='reservation.release', target=f'{row.kind}:{row.number:04d}',
                        detail={'team': team_id, 'reason': 'team stopped'}))


def check(db, team_id: str | None, added: dict[str, set[int]], base_numbers: dict[str, set[int]]) -> list[str]:
    """Problems with the numbers a branch adds: already on the base, or (for a team branch) not reserved by it."""
    lapse(db)
    problems = []
    for kind in KINDS:
        for number in sorted(added.get(kind, set())):
            label = f'{kind} {number:04d}'
            if number in base_numbers.get(kind, set()):
                problems.append(f'{label} already exists on the base branch')
                continue
            if team_id is None:
                continue
            row = db.scalar(select(Reservation).where(Reservation.kind == kind, Reservation.number == number))
            if row is None or row.state not in LIVE or row.team_id != team_id:
                problems.append(f'{label} is not reserved by this team (use reserve)')
    return problems


def view(row: Reservation) -> dict:
    return {'id': row.id, 'kind': row.kind, 'number': row.number, 'label': f'{row.number:04d}', 'state': row.state,
            'branch': row.branch, 'expires_at': row.expires_at.isoformat()}


TEAM_FIELDS: dict[str, set[str]] = {'reserve': {'kind'}, 'release': {'reservation'}}
TEAM_ACTIONS: dict = {}  # registered in gates.py, which owns the team tools that need the broker
ON_STATE: list = []
