"""Review context is complete and bounded; omitted context cannot grant allow."""
import pytest

from acc.session_relay.native_approval import describe_tool


@pytest.mark.parametrize('tool,inputs,workspace',[('unknown',{},'/fixture'),('Bash',[],'/fixture'),
    ('Bash',{'command':'x'*5121},'/fixture'),('Write',{'content':'x'*5121},'/fixture'),('Read',{},None)])
def test_unreviewable_native_action_has_no_allow_context(tool,inputs,workspace):
    assert describe_tool(tool,inputs,workspace) is None


def test_exact_review_includes_all_tool_input_fields():
    rendered=describe_tool('Bash',{'command':'fixture command','timeout':123,'run_in_background':True},'/fixture')
    assert rendered=='Tool: Bash\nDirectory:\n/fixture\nExact input:\n{"command":"fixture command","run_in_background":true,"timeout":123}'
