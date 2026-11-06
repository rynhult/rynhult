import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ChatReader } from '../src/lib/chat-reader.ts';
const page = (state='ready', cursor='good') => ({ session_id:'s', run_id:1, state, cursor, before_cursor:null, historical_run_attribution:'unknown', observed_at:null, items:[], omitted:0, reset:false, capabilities:{ read_history:state==='ready',follow_messages:state==='ready',rich_tools:false } });
const until = async predicate => { for(let i=0;i<200;i++){if(predicate())return;await new Promise(r=>setTimeout(r,1));}throw Error('condition not met'); };
test('error cursor is never reused after pause/resume; busy retries from the same consumed cursor', async () => {
  let full=0; const cursors=[]; const busyErrors=[]; let state; let round=0;
  const fetcher=async url=>{if(!url.includes('/events?')){full++;return Response.json(page());}
    cursors.push(new URL(url,'https://example.com').searchParams.get('after'));
    const value=page(++round===1?'error':round===2?'busy':'ready',round<3?'POISON':'good-2');
    return new Response(`event: chat\ndata: ${JSON.stringify(value)}\n\n`,{headers:{'Content-Type':'text/event-stream'}});};
  const reader=new ChatReader('s','scope',v=>{state=v;if(v.page?.state==='busy')busyErrors.push(v.error);},{fetcher,delay:1});
  reader.start(true);await until(()=>state?.page?.state==='error');reader.stop();reader.state={...reader.state,error:'old transport error'};reader.start(true);
  await until(()=>cursors.length>=3);reader.stop();
  assert.equal(full,1); assert.deepEqual(cursors.slice(0,3),['good','good','good']); assert.ok(busyErrors.length); assert.ok(busyErrors.every(e=>e===null));
});
test('gap stays after ready heartbeat and after older pagination', async()=>{
  let state;let calls=0;
  const fetcher=async url=>!url.includes('/events?')?Response.json({...page(),state:'gap',omitted:5}):new Response(`event: chat\ndata: ${JSON.stringify({...page(),cursor:'next'})}\n\n`,{headers:{'Content-Type':'text/event-stream'}});
  const reader=new ChatReader('s','scope',v=>{state=v;calls++;},{fetcher,delay:1});reader.start(true);
  await until(()=>state?.page?.state==='ready');reader.stop();
  assert.equal(state.page.incomplete,true);assert.equal(state.page.omitted,5);assert.ok(calls>1);
});
