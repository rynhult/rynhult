import { emptyReader, type ReaderState } from './chat-reader.ts';
type Message = { type: 'state'; state: ReaderState } | { type: 'lead'; epoch: number; active: boolean; seed: ReaderState } | { type: 'stop' } | { type: 'action'; action: 'reload' | 'older' };
type Member = { send: (message: Message) => void; visible: boolean; active: boolean; closed: boolean };
type Group = { members: Set<Member>; leader?: Member; active?: boolean; epoch: number; state: ReaderState };
/** Same-origin worker coordination only. Admission/identity is checked by worker
 * wrapper; transcript I/O is confined to the single elected document's reader. */
export class ChatCoordinator {
  private groups = new Map<string, Group>();
  has(key: string) { return this.groups.has(key); }
  get size() { return this.groups.size; }
  join(key: string, scope: string, send: Member['send'], visible: boolean, active: boolean) {
    if (!this.groups.has(key)) this.groups.set(key,{members:new Set(),epoch:0,state:emptyReader(scope)});
    const group=this.groups.get(key)!;
    const member:Member={send,visible,active,closed:false};group.members.add(member);
    const deliver=()=>send({type:'state',state:active?group.state:{...group.state,items:[]}});
    deliver();
    const elect=()=>{
      const candidates=[...group.members].filter(m=>m.visible);
      const chosen=candidates.find(m=>m.active)??candidates[0];
      const mode=!!chosen?.active;
      if(group.leader===chosen && group.active===mode)return;
      group.leader?.send({type:'stop'});group.state={...group.state,older:false,following:false};group.leader=chosen;group.active=mode;group.epoch++;
      chosen?.send({type:'lead',epoch:group.epoch,active:mode,seed:group.state});
    };
    elect();
    return {
      visibility:(shown:boolean,reading:boolean)=>{if(member.closed)return;member.visible=shown;member.active=reading;active=reading;
        elect();if(shown)deliver();},
      state:(state:ReaderState,epoch:number)=>{
        if(member.closed || group.leader!==member || epoch!==group.epoch)return;
        group.state=state;
        for(const target of group.members)if(target.active || !state.loaded || (state.error && !state.items.length))target.send({type:'state',state:target.active?state:{...state,items:[]}});
      },
      action:(action:'reload'|'older')=>{if(!member.closed)group.leader?.send({type:'action',action});},
      close:()=>{if(member.closed)return;member.closed=true;group.members.delete(member);elect();
        if(!group.members.size)this.groups.delete(key);},
    };
  }
}
