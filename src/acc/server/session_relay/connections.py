"""Expiring HTTP negotiation/observer attachments. None grants mutation authority."""
import secrets
import threading
import time

from acc.session_relay import protocol as p


class Connections:
    def __init__(self):
        self.entries = {}
        self.lock = threading.Lock()

    def initialize(self, scope, params, *, owner_scope=None, offered=frozenset()):
        negotiated = p.negotiate(params, offered=offered)
        owner_scope = scope if owner_scope is None else owner_scope
        with self.lock:
            now = time.monotonic()
            self.entries = {key: value for key, value in self.entries.items() if value['expires'] > now}
            # A reconnect of this observer replaces its previous negotiation.
            # Distinct tabs have distinct client IDs and retain separate entries.
            self.entries = {key: value for key, value in self.entries.items()
                            if (value['owner_scope'], value['client']) != (owner_scope, params['clientId'])}
            if len(self.entries) >= 512 or sum(e['owner_scope'] == owner_scope for e in self.entries.values()) >= 16:
                raise p.RelayError(p.ErrorCode.LIMIT)
            token = secrets.token_hex(32)
            self.entries[token] = {'scope': scope, 'owner_scope': owner_scope, 'client': params['clientId'], 'expires': now + 3600,
                                   'negotiated': negotiated, 'attachment': None, 'seq': 0}
            return {'major': negotiated.major, 'minor': negotiated.minor, 'features': sorted(negotiated.features), 'connectionToken': token}

    def use(self, token, scope, command):
        with self.lock:
            entry = self.entries.get(token)
            if not entry or entry['scope'] != scope or entry['expires'] <= time.monotonic():
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            params = command.params
            if command.method == 'session.attach':
                if entry['client'] != params['clientId'] or params['connectionSeq'] <= entry['seq']:
                    raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
                entry['seq'] = params['connectionSeq']
                entry['attachment'] = secrets.token_hex(16)
            elif not entry['attachment'] or ('attachmentId' in params and params['attachmentId'] != entry['attachment']):
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            if command.method == 'session.detach':
                if params['connectionSeq'] <= entry['seq']:
                    raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
                del self.entries[token]
            return dict(entry)

    def finish(self,token,owner_scope,attachment):
        """Read-only result after verified run replacement; never renew scope."""
        with self.lock:
            entry=self.entries.get(token)
            if (not entry or entry['owner_scope']!=owner_scope or entry['attachment']!=attachment
                    or entry['expires']<=time.monotonic()):
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
