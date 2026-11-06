import { ChatCoordinator } from './chat-coordinator.ts';
import type { ReaderState } from './chat-reader';
const coordinator=new ChatCoordinator();
const runtime=globalThis as unknown as { onconnect: (event: MessageEvent) => void };
/** Admission is real HTTP, not a caller-asserted person name or a hash capability. */
async function identity(sessionId:string) {
  const options:RequestInit={credentials:'same-origin',cache:'no-store'};
  const auth=await fetch('/api/auth/state',options);
  if(!auth.ok)throw Error('Chat access ended.');
  const person=await auth.json();
  if(!person.authenticated || person.step_up_required || typeof person.username!=='string')throw Error('Chat access ended.');
  const session=await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`,options);
  if(!session.ok)throw Error('Chat access ended.');
  return person.username as string;
}
runtime.onconnect=event=>{
  const port=event.ports[0];let handle:ReturnType<ChatCoordinator['join']>|undefined;
  let closed=false,sessionId='',person='',scope='',last=Date.now(),visibilityVersion=0;
  let desired={visible:false,active:false};
  const close=()=>{if(closed)return;closed=true;handle?.close();clearInterval(timer);port.close();};
  const refuse=(type='denied')=>{port.postMessage({type});close();};
  const timer=setInterval(()=>{if(Date.now()-last>(desired.visible?30000:300000))close();},10000);
  port.onmessage=async message=>{
    const data=message.data;if(!data || typeof data!=='object' || closed)return;
    last=Date.now();
    if(data.type==='close'){close();return;}
    if(data.type==='ping'){port.postMessage({type:'pong'});return;}
    if(data.type==='subscribe' && !handle && !sessionId){
      if(typeof data.sessionId!=='string'||! /^[a-zA-Z0-9-]{1,128}$/.test(data.sessionId)||typeof data.scope!=='string'||data.scope.length>4096){refuse();return;}
      sessionId=data.sessionId;scope=data.scope;desired={visible:!!data.visible,active:!!data.active};
      try{person=await identity(sessionId);if(closed)return;const key=JSON.stringify([person,sessionId,scope]);if(coordinator.size>=16&&!coordinator.has(key)){refuse('capacity');return;}handle=coordinator.join(key,scope,m=>port.postMessage(m),desired.visible,desired.active);}catch{refuse();}return;
    }
    if(data.type==='visibility'){
      desired={visible:!!data.visible,active:!!data.active};const version=++visibilityVersion;
      if(!handle)return;
      if(!data.visible||!data.active){handle.visibility(!!data.visible,false);return;}
      if(data.visible){try{if(await identity(sessionId)!==person){refuse();return;}}catch{refuse();return;}}
      if(!closed&&version===visibilityVersion)handle.visibility(desired.visible,desired.active);
    }else if(!handle)return;
    else if(data.type==='state'){
      const state=data.state as ReaderState;
      if(!state || state.scope!==scope || !Array.isArray(state.items) || state.items.length>1000 ||
        state.items.some(i=>typeof i.text!=='string'||i.text.length>131072)||state.items.reduce((n,i)=>n+i.text.length,0)>2097152){refuse();return;}
      handle.state(state,data.epoch);
    }else if(data.type==='action'&&(data.action==='older'||data.action==='reload'))handle.action(data.action);
  };
  port.onmessageerror=close;port.start();
};
