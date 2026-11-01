"""No OS accounts/services are changed by unprivileged folder worker tests."""
from contextlib import contextmanager


class FixtureHost:
    def __init__(self):
        self.created = False
        self.members = set()
        self.blocked = False
        self.stops = 0

    @contextmanager
    def stopped(self, account):
        self.stops += 1
        if self.blocked:
            raise ValueError('Account processes are still running.')
        yield

    def plan_group(self, operation, account):
        return {'name': 'ryn-repo-' + operation[:16], 'gid': 62345}

    def create_group(self, group, account, save):
        group['created'] = True
        save()
        self.created = True
        group['members_added'] = ['alex', 'alex-agent']
        self.members.update(group['members_added'])
        save()

    def remove_group_members(self, group, account, save):
        self.members.difference_update(group.get('members_added', []))
        group['members_added'] = []
        save()
        return 'Retained empty purpose group for administrator inode review.'

    def verify(self, path, records, account, write, group=None):
        return {'read': True, 'write': write, 'checked': len(records)}
