import {test} from 'node:test';
import assert from 'node:assert/strict';
import { ChatCoordinator } from '../src/lib/chat-coordinator.ts';
import { emptyReader } from '../src/lib/chat-reader.ts';
test('twenty views elect one reader, transfer continuation once, and reject late old-leader frames',()=>{
 const coordinator=new ChatCoordinator();const inboxes=[];const handles=[];
 for(let i=0;i<20;i++){inboxes[i]=[];handles.push(coordinator.join('person/session','scope',m=>inboxes[i].push(m),true,true));}
 assert.equal(inboxes.flat().filter(m=>m.type==='lead').length,1);
 const first=inboxes[0].find(m=>m.type==='lead');
 handles[0].state({...emptyReader('scope'),loaded:true,page:{cursor:'consumed'}},first.epoch);
 handles[0].close();
 const next=inboxes[1].find(m=>m.type==='lead');assert.equal(next.seed.page.cursor,'consumed');
 handles[0].state({...emptyReader('scope'),page:{cursor:'POISON'}},first.epoch);
 handles[1].close();assert.equal(inboxes[2].find(m=>m.type==='lead').seed.page.cursor,'consumed');
 for(const h of handles)h.close();assert.equal(coordinator.size,0);
});
test('hidden views do not receive transcript updates, and their last close releases the reader',()=>{
 const coordinator=new ChatCoordinator();const a=[],b=[];
 const first=coordinator.join('p/s','scope',m=>a.push(m),true,true),second=coordinator.join('p/s','scope',m=>b.push(m),false,false);
 const epoch=a.find(m=>m.type==='lead').epoch;const before=b.length;
 first.state({...emptyReader('scope'),loaded:true,items:[{id:'item',text:'private'}]},epoch);
 assert.equal(b.length,before);
 second.visibility(true,true);assert.equal(b.at(-1).state.items[0].text,'private');
 first.close();second.close();assert.equal(coordinator.size,0);
});
