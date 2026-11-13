"""Pinned command-answer lifecycle correlation. No dispatch or authority here.

A write, request resolution or arbitrary tool result alone is never enough.
Only the exact owned request/item/turn's qualified choice-specific lifecycle can
report application. Other request families and incomplete cleanup stay unknown.
"""
from . import protocol as p


class CommandAnswerEvidence:
    def __init__(self):
        self.entries={}

    def register(self,identifier,request,digest):
        if request.get('method') not in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):return
        params=request['params']
        if not p.valid_request_id(request.get('id')) or not all(p.valid_opaque(params.get(k)) for k in ('threadId','turnId','itemId')):return
        if len(self.entries)>=p.MAX_PENDING_DECISIONS:raise p.RelayError(p.ErrorCode.LIMIT)
        entry={'kind':'fileChange' if request['method']=='item/fileChange/requestApproval' else 'commandExecution','request':request['id'],'thread':params['threadId'],'turn':params['turnId'],
            'item':params['itemId'],'digest':digest,'sent':False,'resolved':False,'item_status':None,
            'item_exit':None,'finished':False,'ambiguous':False,'operation':None,'choice':None,'closed_published':False}
        for other in self.entries.values():
            if (other['request']==entry['request'] or
                    (other['thread'],other['turn'],other['item'])==(entry['thread'],entry['turn'],entry['item'])):
                other['ambiguous']=entry['ambiguous']=True
        self.entries[identifier]=entry

    def sending(self,identifier,operation,choice):
        entry=self.entries.get(identifier)
        if entry is None:return  # unsupported family remains unknown
        if entry['sent'] or entry['resolved'] or entry['finished'] or entry['ambiguous']:
            raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
        entry.update(sent=True,operation=operation,choice=choice)

    def observe(self,event):
        method=event.get('method');params=event.get('params',{});proofs=[]
        for identifier,entry in self.entries.items():
            if entry['ambiguous'] or params.get('threadId')!=entry['thread']:continue
            resolved=(method=='serverRequest/resolved' and type(params.get('requestId')) is type(entry['request'])
                and params.get('requestId')==entry['request'])
            if entry['finished']:
                if resolved and not entry['sent']:entry['resolved']=True
                if not entry['sent'] and entry['resolved'] and not entry['closed_published']:
                    entry['closed_published']=True
                    proofs.append({'kind':'decision.closed','decisionId':identifier,'actionDigest':entry['digest']})
                continue
            if resolved:
                if entry['resolved']:entry['ambiguous']=True
                entry['resolved']=True
            elif (method=='item/completed' and params.get('turnId')==entry['turn']
                    and params.get('item',{}).get('id')==entry['item']):
                item=params['item']
                if item.get('type')!=entry['kind'] or entry['item_status'] is not None:
                    entry['ambiguous']=True
                else:
                    entry['item_status']=item.get('status')
                    entry['item_exit']=item.get('exitCode')
            elif method=='turn/completed' and params.get('turn',{}).get('id')==entry['turn']:
                entry['finished']=True
                if not entry['sent'] and entry['resolved']:
                    entry['closed_published']=True
                    proofs.append({'kind':'decision.closed','decisionId':identifier,'actionDigest':entry['digest']})
                status=params['turn'].get('status')
                choice=entry['choice']
                executed=(entry['item_status']=='completed' or (entry['item_status']=='failed' and entry['kind']=='fileChange') or (entry['item_status']=='failed'
                    and type(entry['item_exit']) is int and entry['item_exit']!=0))
                applied=((choice=='accept' and executed and status=='completed')
                    or (choice=='decline' and entry['kind']=='fileChange' and entry['item_status']=='declined' and status=='completed')
                    or (choice=='cancel' and entry['item_status']=='declined' and status=='interrupted'))
                if not entry['sent'] or not entry['resolved'] or not applied:continue
                operation=entry['operation']
                ref=p.sha256_digest(p.canonical_json([entry['request'],entry['thread'],entry['turn'],
                    entry['item'],choice,entry['item_status'],entry['item_exit'],status]))
                proofs.append({'kind':'decision.applied','decisionId':identifier,
                    'operationId':operation.operation_id,'choiceId':choice,'actionDigest':entry['digest'],
                    'nativeRef':ref})
        return proofs
