"""Private original-decision checkpoints; no provider input or launch operation."""
import json
import os
import stat
from dataclasses import asdict

from acc.session_relay import protocol as p

from .claude_admission import MAX_HISTORY, MAX_RECORDS
from .claude_tool_binding import Completion, prompt_digest
from .session_relay import AdmissionJournal

CLAUDE_DURABLE_EVIDENCE_QUALIFIED = False


def checkpoint(reader):
    reader._validate()
    if reader.offset is None or reader._hash(reader.offset) != reader.prefix:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    return {'project': reader.project_name, 'nativeId': reader.native_id, 'offset': reader.offset,
        'prefix': reader.prefix.hex(), 'root': [reader.root_identity.st_dev,reader.root_identity.st_ino],
        'projectIdentity': [reader.project_identity.st_dev,reader.project_identity.st_ino],
        'file': [reader.identity.st_dev,reader.identity.st_ino]}


def restore(root, value):
    from .claude_allow_evidence import NativeAllow
    if (not isinstance(value,dict) or value.keys()!={'project','nativeId','offset','prefix','root','projectIdentity','file'}
            or not isinstance(value['project'],str) or '/' in value['project'] or value['project'] in ('','.','..')
            or not p.valid_opaque(value['nativeId']) or type(value['offset']) is not int
            or not 0 <= value['offset'] <= MAX_HISTORY or not isinstance(value['prefix'],str)
            or len(value['prefix'])!=64):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    reader=NativeAllow(root,root/value['project']/(value['nativeId']+'.jsonl'),value['nativeId'])
    try:
        for info,key in [(reader.root_identity,'root'),(reader.project_identity,'projectIdentity'),(reader.identity,'file')]:
            if [info.st_dev,info.st_ino]!=value[key]:raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        reader.offset=value['offset'];reader.prefix=bytes.fromhex(value['prefix']);reader.nodes={}
        if reader._validate()<reader.offset or reader._hash(reader.offset)!=reader.prefix:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        raw=os.pread(reader.fd,reader.offset,0)
        if len(raw)!=reader.offset or raw and not raw.endswith(b'\n'):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        lines=raw.splitlines()
        if len(lines)>MAX_RECORDS:raise p.RelayError(p.ErrorCode.LIMIT)
        for line in lines:
            row=json.loads(line)
            if not isinstance(row,dict):raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            if row.get('uuid') is None:continue
            identifier,row=reader.node(row)
            if identifier in reader.nodes or row.get('sessionId')!=reader.native_id or row.get('isSidechain') is not False:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            reader.nodes[identifier]=row
        reader._validate()
        if reader._hash(reader.offset)!=reader.prefix:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        return reader
    except BaseException:
        reader.close();raise


class DecisionEvidenceJournal:
    def __init__(self,root,*,capacity=16):
        self.journal=AdmissionJournal(root);self.capacity=capacity
        self.identity=(root/'admission.sqlite3').lstat()
        self.db=self.journal.db
        self.db.execute('CREATE TABLE IF NOT EXISTS decision_evidence (operation TEXT PRIMARY KEY, record TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, completion TEXT, reference TEXT, settled INTEGER NOT NULL DEFAULT 0)')

    def check(self):
        self.journal.check_path()
        info=(self.journal.root/'admission.sqlite3').lstat()
        if ((info.st_dev,info.st_ino)!=(self.identity.st_dev,self.identity.st_ino) or info.st_uid!=os.getuid()
                or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)

    def prepare(self,scope,record):
        self.check()
        binding=p.Binding(**scope)
        if binding.provider!='claude' or not all(isinstance(v,str) and v for v in scope.values()):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if record.keys() != {'event','runId','reader','binding','operation'}:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        key=record['event']['operationId']
        if (not p.valid_opaque(key) or record['event'].get('kind')!='decision.applied'
                or record['event'].get('choiceId')!='accept' or not p.valid_digest(record['event'].get('actionDigest'))
                or not p.valid_opaque(record['runId']) or record['reader']['nativeId']!=scope['native_conversation']):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        operation=record['operation']
        if (operation.get('operationId')!=key or operation.get('runId')!=record['runId']
                or operation.get('method')!='decision.reply' or not p.valid_digest(operation.get('payloadDigest'))
                or not isinstance(operation.get('actor'),dict)
                or record['event'].keys() != {'kind','decisionId','operationId','choiceId','actionDigest'}):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        actor=operation['actor']
        try:
            p.Actor(p.ActorKind(actor['kind']),actor['principal'],p.Channel(actor['channel']),actor.get('device'))
        except (KeyError,ValueError,TypeError) as error:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE) from error
        if (actor.keys()-{'kind','principal','channel','device'} or not p.valid_opaque(actor['principal'])
                or record['binding'].keys()!={'toolId','promptId','toolName','promptDigest'}
                or not all(p.valid_opaque(record['binding'][k]) for k in ('toolId','promptId','toolName'))
                or not p.valid_digest(record['binding']['promptDigest'])
                or not p.valid_opaque(record['event']['decisionId'])):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        text=p.canonical_json({'scope':scope,**record}).decode()
        if len(text.encode())>p.MAX_FRAME_BYTES:raise p.RelayError(p.ErrorCode.LIMIT)
        old=self.db.execute('SELECT record FROM decision_evidence WHERE operation=?',(key,)).fetchone()
        if old:
            if old[0]!=text:raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            return key
        if self.db.execute('SELECT COUNT(*) FROM decision_evidence WHERE settled=0').fetchone()[0]>=self.capacity:
            raise p.RelayError(p.ErrorCode.LIMIT)
        with self.db:self.db.execute('INSERT INTO decision_evidence(operation,record) VALUES(?,?)',(key,text))
        return key

    def sent(self,scope,tool,digest):
        self.check()
        matches=[key for key,value,_,_ in self.pending(scope,prepared=True)
            if value['binding']['toolId']==tool and value['binding']['promptDigest']==digest]
        if len(matches)!=1:raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        with self.db:self.db.execute('UPDATE decision_evidence SET sent=1 WHERE operation=?',(matches[0],))

    def observe(self,scope,payload):
        event=payload.get('hook_event_name');digest=prompt_digest(payload)
        if (event not in ('PostToolUse','PostToolUseFailure') or digest is None
                or payload.get('is_interrupt',False) is not False):return
        tool=payload.get('tool_use_id')
        if not p.valid_opaque(tool):return
        completion=Completion(payload['session_id'],tool,digest,event,
            p.sha256_digest(p.canonical_json([payload['session_id'],digest,tool,event])))
        self.complete(scope,completion)

    def complete(self,scope,completion):
        self.check()
        if (not isinstance(completion,Completion) or completion.event not in ('PostToolUse','PostToolUseFailure')
                or not p.valid_digest(completion.reference)):raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        for key,raw,old in self.db.execute('SELECT operation,record,completion FROM decision_evidence WHERE settled=0 AND sent=1').fetchall():
            value=json.loads(raw);binding=value['binding']
            if (value['scope']!=scope or completion.session!=scope['native_conversation']
                    or completion.tool!=binding['toolId'] or completion.digest!=binding['promptDigest']):continue
            encoded=p.canonical_json(asdict(completion)).decode()
            if old is not None and old!=encoded:raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            with self.db:self.db.execute('UPDATE decision_evidence SET completion=? WHERE operation=?',(encoded,key))

    def pending(self,scope,*,prepared=False):
        self.check()
        return [(key,json.loads(raw),json.loads(completion) if completion else None,reference)
            for key,raw,completion,reference in self.db.execute('SELECT operation,record,completion,reference FROM decision_evidence WHERE settled=0 AND (sent=1 OR ?=1) ORDER BY operation',(int(prepared),))
            if json.loads(raw)['scope']==scope]

    def verified(self,key,reference):
        self.check()
        if not p.valid_digest(reference):raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        old=self.db.execute('SELECT reference FROM decision_evidence WHERE operation=?',(key,)).fetchone()
        if old is None or old[0] is not None and old[0]!=reference:raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        with self.db:self.db.execute('UPDATE decision_evidence SET reference=? WHERE operation=?',(reference,key))

    def settled(self,key):
        self.check()
        with self.db:
            self.db.execute('UPDATE decision_evidence SET settled=1 WHERE operation=? AND reference IS NOT NULL',(key,))
            self.db.execute('DELETE FROM decision_evidence WHERE operation IN (SELECT operation FROM decision_evidence WHERE settled=1 ORDER BY operation LIMIT MAX(0,(SELECT COUNT(*) FROM decision_evidence WHERE settled=1)-128))')

    def close(self):self.journal.close()


async def reconcile(journal,scope,root,publish,*,run_id=None):
    """Read original evidence and republish its immutable event; never answer."""
    count=0
    for key,record,completion,reference in journal.pending(scope):
        if run_id is not None and record['runId'] != run_id:continue
        reader=restore(root,record['reader'])
        try:
            if reference is None:
                reference=reader.applied(record['binding'],completion=Completion(**completion) if completion else None)
        finally:reader.close()
        if reference is None:continue
        journal.verified(key,reference)
        event={**record['event'],'nativeRef':reference,'runId':record['runId'], 'originalOperation':record['operation']}
        if await publish(event) is True:
            journal.settled(key);count+=1
    return count
