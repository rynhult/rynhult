import { useEffect, useRef, useState } from 'react';
import { ChatReader, emptyReader, type ReaderState } from '../lib/chat-reader';
/** One elected document per person/session/source across tabs. Worker admission
 * checks real person + session access; no transcript storage or query cache. */
export function useSessionChat(sessionId:string,scope:string,active:boolean) {
  const [state,setState]=useState(()=>emptyReader(scope));
  const port=useRef<MessagePort|null>(null),visibility=useRef(active);visibility.current=active;
  const reconnect=useRef<()=>void>(()=>{});
  const [foreground,setForeground]=useState(()=>!document.hidden);
  useEffect(()=>{
    let disposed=false,owned:ChatReader|null=null,lastPong=Date.now();
    const publish=(next:ReaderState)=>setState(previous=>{
      const old=new Map(previous.items.map(i=>[i.id,i]));
      const items=next.items.map(i=>{const before=old.get(i.id);return before?.revision===i.revision&&before.text===i.text?before:i;});
      const unchanged=items.length===previous.items.length&&items.every((i,n)=>i===previous.items[n]);
      return {...next,items:unchanged?previous.items:items};
    });
    const fail=(message='You no longer have access to this Chat.')=>{owned?.stop();owned=null;port.current?.postMessage({type:'close'});port.current?.close();port.current=null;publish({...emptyReader(scope),loading:false,error:message});};
    const connect=()=>{
      owned?.stop();owned=null;port.current?.postMessage({type:'close'});port.current?.close();port.current=null;
      publish(emptyReader(scope));
      if(typeof SharedWorker==='undefined') {publish({...emptyReader(scope),loading:false,error:'This browser cannot share a safe Chat reader. Use Terminal.'});return;}
      let worker: SharedWorker;
      try { worker=new SharedWorker(new URL('../lib/chat-worker.ts',import.meta.url),{type:'module'}); } catch {
        publish({...emptyReader(scope),loading:false,error:'This browser could not start a safe Chat reader. Use Terminal.'});return;
      }
      const channel=worker.port;port.current=channel;lastPong=Date.now();
      channel.onmessage=event=>{if(disposed||channel!==port.current)return;const message=event.data;
        if(message.type==='pong'){lastPong=Date.now();return;}
        if(message.type==='denied'){fail();return;}
        if(message.type==='capacity'){fail('Too many Chat sources are open in this browser. Close another session page, then reload Chat.');return;}
        if(message.type==='stop'){owned?.stop();owned=null;return;}
        if(message.type==='state'){publish(message.state);return;}
        if(message.type==='lead'){
          owned?.stop();
          owned=new ChatReader(sessionId,scope,value=>channel.postMessage({type:'state',state:value,epoch:message.epoch}),{seed:message.seed});
          owned.start(message.active);
        }else if(message.type==='action'){if(message.action==='reload')owned?.reload();else void owned?.loadOlder();}
      };
      channel.onmessageerror=()=>fail();worker.onerror=()=>fail();channel.start();
      channel.postMessage({type:'subscribe',sessionId,scope,visible:!document.hidden,active:visibility.current&&!document.hidden});
    };
    const visible=()=>{setForeground(!document.hidden);if(!document.hidden&&Date.now()-lastPong>90000)connect();else port.current?.postMessage({type:'visibility',visible:!document.hidden,active:visibility.current&&!document.hidden});};
    reconnect.current=connect;connect();document.addEventListener('visibilitychange',visible);
    const heartbeat=setInterval(()=>port.current?.postMessage({type:'ping'}),10000);
    return()=>{reconnect.current=()=>{};disposed=true;clearInterval(heartbeat);document.removeEventListener('visibilitychange',visible);owned?.stop();port.current?.postMessage({type:'close'});port.current?.close();port.current=null;};
  },[sessionId,scope]);
  useEffect(()=>{port.current?.postMessage({type:'visibility',visible:!document.hidden,active:active&&!document.hidden});},[active,scope]);
  return {...(state.scope===scope?state:emptyReader(scope)),paused:!active||!foreground,
    loadOlder:()=>port.current?.postMessage({type:'action',action:'older'}),reload:()=>{if(port.current)port.current.postMessage({type:'action',action:'reload'});else reconnect.current();}};
}
export type SessionChatState=ReturnType<typeof useSessionChat>;
