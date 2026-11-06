import { acceptChatPage, canFollow, chatEvents, chatPage, mergeChatItems, type ChatItem, type ChatPage } from './chat.ts';
export type ReaderState = { scope: string; items: ChatItem[]; page?: ChatPage; before: string | null;
  loading: boolean; older: boolean; following: boolean; error: string | null; reset: boolean; limit: boolean; toolNotices: number; loaded: boolean };
export const emptyReader = (scope: string): ReaderState => ({ scope, items: [], before: null, loading: true, older: false, following: false, error: null, reset: false, limit: false, toolNotices: 0, loaded: false });
class ReadError extends Error { status: number; constructor(status: number) { super('Chat read refused.'); this.status = status; } }
const denied = (e: unknown) => e instanceof ReadError && [401,403,404].includes(e.status);
/** The elected visible document owns this cancellable reader. Worker coordination
 * shares its snapshot/consumed cursor, never fetches transcript bytes independently. */
export class ChatReader {
  state: ReaderState;
  private abort: AbortController | null = null;
  private olderAbort: AbortController | null = null;
  private mode: boolean | null = null;
  private fetcher: typeof fetch;
  private delay: number;
  private url: string;
  private emit: (value: ReaderState) => void;
  constructor(sessionId: string, scope: string, emit: (value: ReaderState) => void,
    options: { fetcher?: typeof fetch; delay?: number; seed?: ReaderState } = {}) {
    this.emit = emit;
    this.state = options.seed ?? emptyReader(scope); this.fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis); this.delay = options.delay ?? 2000;
    this.url = `/api/sessions/${encodeURIComponent(sessionId)}/chat`;
  }
  private update(change: Partial<ReaderState>) { this.state = { ...this.state, ...change }; this.emit(this.state); }
  private async read(signal: AbortSignal, limit=100, before?: string) {
    const response = await this.fetcher(`${this.url}?${new URLSearchParams({limit:String(limit),...(before?{before}:{})})}`, {signal,credentials:'same-origin',cache:'no-store'});
    if (!response.ok) throw new ReadError(response.status);
    const page = chatPage(await response.json());
    if (page.session_id !== decodeURIComponent(this.url.split('/')[3])) throw new ReadError(409);
    return page;
  }
  private pause(signal: AbortSignal, ms=this.delay) {
    return new Promise<void>(resolve=>{const done=()=>{clearTimeout(timer);signal.removeEventListener('abort',done);resolve();};const timer=setTimeout(done,ms);signal.addEventListener('abort',done,{once:true});if(signal.aborted)done();});
  }
  stop() { this.abort?.abort(); this.abort=null; this.olderAbort?.abort(); this.mode=null; }
  start(active: boolean) {
    if (this.mode === active) return;
    this.stop(); this.mode=active;
    if (this.state.reset || this.state.limit || (!active && this.state.loaded)) { this.update({following:false}); return; }
    const abort=new AbortController();this.abort=abort;
    const live=()=>!abort.signal.aborted;
    void (async()=>{
      if (!active) {
        do {
          const next=await this.read(abort.signal,1); if(!live())return;
          this.update({page:acceptChatPage(this.state.page,next),loading:false});
          if(next.capabilities.read_history || next.state==='unsupported')return;
          await this.pause(abort.signal,next.state==='busy'?this.delay:15000);
        } while(live());return;
      }
      let backoff=this.delay;
      while(live()) {
        if (!this.state.loaded || !this.state.page?.cursor) {
          this.update({loading:true,error:null});
          const next=await this.read(abort.signal);if(!live())return;
          const accepted=acceptChatPage(this.state.page,next,true);
          if (['error','offline','busy'].includes(next.state)) {
            this.update({page:accepted,loading:false,following:false,error:null});
            if(next.state!=='busy')return;
            await this.pause(abort.signal,backoff);backoff=Math.min(30000,backoff*2);continue;
          }
          this.update({items:next.items,page:accepted,before:next.before_cursor,loading:false,loaded:true,toolNotices:next.notices?.tools??0});
          if(!canFollow(next))return;
        }
        const cursor=this.state.page!.cursor;
        const response=await this.fetcher(`${this.url}/events?${new URLSearchParams({after:cursor})}`,{signal:abort.signal,credentials:'same-origin',cache:'no-store',headers:{Accept:'text/event-stream'}});
        if(!response.ok)throw new ReadError(response.status);
        let busy=false;
        for await(const next of chatEvents(response)) {
          if(!live())return;
          if(next.session_id!==this.state.page!.session_id || next.run_id!==this.state.page!.run_id || next.reset)throw new ReadError(409);
          const accepted=acceptChatPage(this.state.page,next);
          if(['error','offline','busy'].includes(next.state)) {
            this.update({page:accepted,following:false,loading:false,error:null});
            if(next.state==='busy'){busy=true;break;}return;
          }
          const result=mergeChatItems(this.state.items,next.items,'follow');
          this.update({items:result.items,page:{...accepted,cursor:result.overflow?cursor:accepted.cursor},following:canFollow(next),loading:false,limit:result.overflow,toolNotices:Math.max(this.state.toolNotices,next.notices?.tools??0),error:null});
          if(result.overflow || !canFollow(next))return;
        }
        if(!live())return;
        this.update({following:false});
        // A new follow request performs server authority/run checks itself. Do not
        // add a full or metadata file read at every normal 25-second EOF.
        await this.pause(abort.signal,busy?backoff:this.delay);
        backoff=busy?Math.min(30000,backoff*2):this.delay;
      }
    })().catch(e=>{if(live())this.failed(e);});
  }
  private failed(e: unknown) {
    const reset=e instanceof ReadError && [400,409].includes(e.status);
    this.update({...(denied(e)?{items:[],page:undefined,before:null,toolNotices:0,loaded:false}:{}),loading:false,older:false,following:false,reset,
      error:denied(e)?'You no longer have access to this Chat.':reset?'The source or session run changed. Reload Chat to read a fresh snapshot.':'Chat disconnected. Retained messages may be stale. Retry or open Terminal.'});
  }
  reload() { const active=this.mode??true;this.stop();this.state=emptyReader(this.state.scope);this.emit(this.state);this.start(active); }
  async loadOlder() {
    if(!this.state.before || this.state.older || this.state.reset || this.state.limit)return;
    const abort=new AbortController();this.olderAbort?.abort();this.olderAbort=abort;this.update({older:true});
    try {
      const next=await this.read(abort.signal,100,this.state.before);if(abort.signal.aborted)return;
      if(next.run_id!==this.state.page?.run_id || next.reset)throw new ReadError(409);
      if(['busy','error','offline'].includes(next.state)){this.update({page:acceptChatPage(this.state.page,next)});return;}
      const result=mergeChatItems(this.state.items,next.items,'older');
      // Older pages can increase known incompleteness but cannot move follow position.
      const page=acceptChatPage(this.state.page,next);page.cursor=this.state.page?.cursor??'';
      this.update({items:result.items,page,before:result.overflow?this.state.before:next.before_cursor,limit:result.overflow,toolNotices:Math.max(this.state.toolNotices,next.notices?.tools??0)});
    } catch(e) {if(!abort.signal.aborted){if(denied(e)||e instanceof ReadError&&[400,409].includes(e.status))this.stop();this.failed(e);}}
    finally {if(!abort.signal.aborted)this.update({older:false});}
  }
}
