import {test} from 'node:test';
import assert from 'node:assert/strict';
import {recentResults} from '../src/lib/recentResults.ts';

const doc=(path,modified)=>({path,modified,bytes:12,commit:null,committed_at:null,subject:null});
test('newest five notes mix result sources, ignore designs and keep update time separate from commit time',()=>{
  const knowledge={repo:null,docs:{
    handoffs:[doc('docs/handoffs/old.md',10),doc('docs/handoffs/new.md',90)],
    council:[doc('docs/council/review.md',80)],
    research:[doc('docs/research/finding.md',70),doc('docs/research/next.md',60),{...doc('docs/research/fifth.md',50),committed_at:'2099-01-01T00:00:00Z'}],
    design:[doc('docs/design/newest.md',100)],decisions:[doc('docs/decisions/new.md',110)],
  }};
  assert.deepEqual(recentResults(knowledge,[],7).map(r=>r.doc.path),[
    'docs/handoffs/new.md','docs/council/review.md','docs/research/finding.md','docs/research/next.md','docs/research/fifth.md',
  ]);
});
test('attribution requires an unambiguous whole session ID visible in this project; archived sessions remain attributable',()=>{
  const sessions=[{id:'abc123',project_id:7,name:'Visible',desired_state:'archived'},{id:'def456',project_id:8,name:'Other project'},{id:'fed321',project_id:7,name:'Second visible'}];
  const rows=recentResults({repo:null,docs:{handoffs:[
    doc('docs/handoffs/2026-11-01-abc123-result.md',90),doc('docs/handoffs/xabc123x.md',80),
    doc('docs/handoffs/def456.md',70),doc('docs/handoffs/abc123-fed321.md',60),doc('docs/handoffs/abc123-def456.md',50),
  ]}},sessions,7);
  assert.deepEqual(rows.map(r=>r.session?.name??null),['Visible',null,null,null,'Visible']);
});
test('missing time is never invented and ties have a stable path order',()=>{
  const rows=recentResults({repo:null,docs:{research:[doc('docs/research/b.md',5),doc('docs/research/a.md',5),doc('docs/research/missing.md',NaN),doc('docs/research/zero.md',0)]}},[],7);
  assert.deepEqual(rows.map(r=>[r.doc.path,r.updated]),[
    ['docs/research/a.md','1970-01-01T00:00:05.000Z'],['docs/research/b.md','1970-01-01T00:00:05.000Z'],
    ['docs/research/missing.md',null],['docs/research/zero.md',null],
  ]);
});
