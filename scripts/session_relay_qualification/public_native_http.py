"""Real production cookie/CSRF/router binding, inside the offline native lab."""
from dataclasses import replace
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from acc.server import auth
from acc.server.api.context import Context
from acc.server.api.session_relay import router
from acc.server.db import write_intent
from acc.server.models import AuthSession, User


class Public:
    @classmethod
    async def create(cls,c,manager,sid,*,features=None):
        self=cls();self.c,self.sid=c,sid
        @write_intent
        def login():
            with c.db() as db:
                user=db.get(User,1)
                user.account=c.resolve()[0].account
                token,csrf=auth.create_session(db,user,'offline-native-public-fixture')
                db.commit()
                return token,csrf
        self.cookie,csrf=login()
        settings=replace(c.ctx.settings,cookie_secure=False,allowed_origins=('http://example.lan',))
        ctx=Context(settings,c.db,c.ctx.supervisor,c.ctx.broker,None,'offline-service-token')
        ctx.relay_runtime=manager
        if not hasattr(ctx.supervisor.vault,'redactor'):
            ctx.supervisor.vault.redactor=lambda *_:SimpleNamespace(text=lambda value:value)
        app=FastAPI();app.include_router(router(ctx))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://example.lan',
            cookies={ctx.session_cookie:self.cookie},headers={'x-acc-csrf':csrf,'origin':'http://example.lan'})
        value=(await self.call('initialize',{'major':1,'minor':0,'features':features if features is not None else ['writer','terminal'],
            'clientId':'offline-public-native'}))['result']
        self.client.headers['x-acc-relay-connection']=value['connectionToken']
        self.view=(await self.call('session.attach',{'clientId':'offline-public-native','connectionSeq':1}))['result']
        assert self.view['snapshot']['terminalMediated'] is False
        return self

    async def call(self,method,params):
        body={'jsonrpc':'2.0','id':'offline-public','method':'ry/'+method,
            'params':params if method=='initialize' else {'sessionId':self.sid,**params}}
        response=await self.client.post(f'/api/sessions/{self.sid}/relay',json=body)
        assert response.status_code==200,response.text
        return response.json()

    @write_intent
    def revoke(self):
        with self.c.db() as db:
            db.delete(db.get(AuthSession,auth.digest(self.cookie)));db.commit()

    async def close(self):await self.client.aclose()
