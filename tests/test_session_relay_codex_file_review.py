import pytest

from acc.session_relay import protocol as p
from acc.session_relay.adapters.codex import CodexAdapter
from acc.session_relay.native_approval import describe_file


def test_file_review_is_copied_exact_and_invalidated_by_mutation_or_duplicate_native_item():
    adapter=CodexAdapter.__new__(CodexAdapter);adapter.file_items={}
    changes=[{'path':'/tmp/fixture','kind':{'type':'add'},'diff':'original\n'}]
    event={'method':'item/started','params':{'threadId':'t','turnId':'u','item':{'type':'fileChange','id':'i','changes':changes}}}
    request={'params':{'threadId':'t','turnId':'u','itemId':'i','grantRoot':None}}
    adapter.observe_file(event);changes[0]['diff']='mutated external object'
    assert adapter.file_review(request)[0]['diff']=='original\n'
    adapter.observe_file({'method':'item/fileChange/patchUpdated','params':request['params']})
    with pytest.raises(p.RelayError):adapter.file_review(request)
    adapter.file_items={};adapter.observe_file(event);adapter.observe_file(event)
    with pytest.raises(p.RelayError):adapter.file_review(request)


@pytest.mark.parametrize('changes',[None,[],[{'path':'relative','kind':{'type':'add'},'diff':'x'}],
    [{'path':'/tmp/x','kind':{'type':'add'},'diff':'x','hidden':'unreviewed'}],
    [{'path':'/tmp/x','kind':{'type':'add'},'diff':'x'*6000}]])
def test_missing_unbounded_or_unreviewable_patch_never_becomes_an_approval(changes):
    with pytest.raises(p.RelayError):describe_file(changes)
