"""Fixed offline installed-guard qualification, using the enrolled native lab."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sysconfig
import tempfile
from pathlib import Path


def capture_installed_security(provider='codex', tampered=False, owned_view=False, *, scenario='security', bridge=None, session_id=None):
    if provider not in ('codex', 'claude') or type(tampered) is not bool or type(owned_view) is not bool:
        raise ValueError('Unsupported installed security fixture')
    if provider == 'claude' and owned_view:
        raise ValueError('Claude Terminal integration belongs to the primary worker')
    scenarios = {'stock_race_supervisor_recover_new':'codex','stock_race_fresh_codex_owner_recover_new':'codex','stock_race_fresh_codex_owner_applied_recover_new':'codex','stock_race_fresh_codex_transport_applied_recover_new':'codex','stock_race_expiry_recover_new':'codex','stock_race_deadline_recover_new':'codex','stock_race_retired_recover_new':'codex','stock_race_cancel_applied':'codex','stock_race_chat_loser':'codex','stock_race_cancel_recover_new':'codex','stock_race_owner_after_recover_new':'codex','stock_race_transport_after_recover_new':'codex','stock_race_revoke_recover_new':'codex','stock_race_owner_recover_new':'codex','stock_race_transport_recover_new':'codex','stock_cancel_lost_before':'codex','stock_cancel_lost_after':'codex','stock_concurrent_native_first':'codex','stock_concurrent_chat_first':'codex','stock_concurrent_revoked_native':'codex','stock_public_failed':'codex','stock_public_lost_before':'codex','stock_public_lost_after':'codex','stock_post_chat':'codex','stock_post_expiry':'codex','stock_post_cancel':'codex','stock_post_revoke':'codex','stock_lifecycle_chat':'codex','stock_lifecycle_expiry':'codex','stock_lifecycle_revoke':'codex','stock_public_cancel':'codex','stock_public':'codex','stock_withdrawal':'codex','stock_decoder_concurrent':'codex','stock_decoder':'codex','security':None, 'decisions':'claude', 'controls':'codex', 'selector':'codex', 'handover':'claude',
                 'decision_expiry':'claude', 'frozen_native':'claude', 'tui_expiry':'claude', 'tui_deny_ack':'claude', 'tui_deny_ack_lost':'claude','tui_allow_inventory':'claude','tui_allow_ack':'claude','tui_allow_reconcile':'claude','tui_allow_late_completion':'claude','tui_allow_execution_prepared':'claude','tui_allow_execution_sent':'claude','tui_allow_execution_completion':'claude','tui_allow_execution_lost_ack':'claude','tui_allow_execution_loss_before':'claude','tui_allow_execution_loss_after':'claude','tui_allow_execution_revoke_before':'claude','tui_allow_execution_revoke_after':'claude','tui_allow_execution_interrupt':'claude','tui_allow_restart':'claude','tui_allow_failed':'claude','tui_native_prompt':'claude','tui_allow_read':'claude','tui_allow_write':'claude','tui_allow_edit':'claude','tui_read_concurrent':'claude','tui_write_deny':'claude','tui_edit_deny':'claude', 'public_controls':'claude', 'roundtrip':'claude', 'repeat_activation':'claude', 'public_decisions':'claude', 'decision_wire':'claude', 'failed_target':'claude', 'failed_target_post':'claude', 'retained_draft':'claude', 'failed_sdk_target':'claude', 'failed_sdk_connect':'claude', 'retained_public':'claude', 'explicit_recovery':'claude', 'explicit_recovery_pre':'claude', 'explicit_recovery_sdk':'claude', 'explicit_recovery_sdk_connect':'claude', 'explicit_recovery_terminal':'claude', 'explicit_recovery_lost_owned':'claude', 'public_l1':'claude', 'codex_ack':'codex', 'codex_ack_interrupt':'codex', 'codex_ack_accept':'codex', 'codex_ack_lost_before':'codex', 'codex_ack_lost_after':'codex', 'codex_ack_identical':'codex', 'codex_ack_failed':'codex', 'codex_ack_failed_lost':'codex', 'codex_file_inventory':'codex', 'codex_file_ack':'codex', 'codex_file_decline':'codex', 'codex_file_interrupt':'codex', 'codex_file_lost_before':'codex', 'codex_file_lost_after':'codex', 'codex_file_failed':'codex', 'codex_file_failed_lost':'codex', 'codex_file_failed_before':'codex', 'codex_file_cancel_refused':'codex', 'claude_ack':'claude', 'claude_ack_allow':'claude', 'claude_ack_interrupt':'claude', 'claude_ack_lost_before':'claude', 'claude_ack_lost_after':'claude', 'claude_ack_read_allow':'claude', 'claude_ack_read_deny':'claude', 'claude_ack_read_interrupt':'claude', 'claude_ack_read_lost_before':'claude', 'claude_ack_read_lost_after':'claude', 'claude_ack_write_allow':'claude', 'claude_ack_write_deny':'claude', 'claude_ack_write_interrupt':'claude', 'claude_ack_write_lost_before':'claude', 'claude_ack_write_lost_after':'claude', 'claude_ack_edit_allow':'claude', 'claude_ack_edit_deny':'claude', 'claude_ack_edit_interrupt':'claude', 'claude_ack_edit_lost_before':'claude', 'claude_ack_edit_lost_after':'claude', 'claude_unoffered_glob':'claude', 'claude_unoffered_grep':'claude', 'claude_ack_read_failed':'claude', 'claude_ack_read_failed_lost':'claude', 'claude_ack_write_failed':'claude', 'claude_ack_write_failed_lost':'claude', 'claude_ack_edit_failed':'claude', 'claude_ack_edit_failed_lost':'claude'}
    scenarios.update({name:'claude' for name in ('tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new',)})
    scenarios.update({'tui_allow_parallel':'claude','tui_allow_ambiguous':'claude'})
    scenarios['codex_private_read']='codex'
    if scenario not in scenarios or scenario != 'security' and (provider != scenarios[scenario] or tampered or owned_view):
        raise ValueError('Unsupported enrolled qualification scenario')
    if scenario == 'selector':
        bridge = Path(bridge).resolve(strict=True)
        if not bridge.is_relative_to('/tmp') or not bridge.is_dir() or not re.fullmatch('[a-f0-9]{12}', session_id or ''):
            raise ValueError('Selector requires a disposable tmp bridge and synthetic session ID')
    elif bridge is not None or session_id is not None:
        raise ValueError('Only the selector fixture supports a bridge')
    root = Path(__file__).resolve().parents[2]
    dependencies = Path(sysconfig.get_path('purelib')).resolve()
    if not dependencies.is_relative_to(root / '.acc'):
        raise RuntimeError('Use the self-contained locked project test environment')
    spec = importlib.util.spec_from_file_location('installed_security_isolation', Path(__file__).with_name('probe.py'))
    isolation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolation)
    source_path = Path(__file__).with_name('enrolled_partition.py')
    record = json.loads((root / 'docs/handoffs/2026-11-11-relay-r1-group8-native-20.json').read_text())
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == record['source_sha256'][str(source_path.relative_to(root))]
    source = source_path.read_text()
    def replace_once(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise RuntimeError('Enrolled fixture interface changed: ' + old[:80])
        source = source.replace(old, new)
    replace_once('    work.mkdir()\n', '    work.mkdir()\n    from installed_security import prepare\n    security = await prepare(root, work, c)\n')
    if scenario != 'security':
        replace_once('from installed_security import prepare', f'from enrolled_{scenario} import prepare')
    if scenario == 'selector':
        replace_once("SID = 'abc123def456'", f'SID = {session_id!r}')
    if source.count("hook_path='/usr/bin/true'") != 2:
        raise RuntimeError('Fixture installation interface changed')
    source = source.replace("hook_path='/usr/bin/true'", 'hook_path=str(security.hook)')
    replace_once('delivery = SimpleNamespace(env={}, catalog=None', 'delivery = SimpleNamespace(env=security.env, catalog=None')
    replace_once("session.launch_spec.files = {'guardrails.json':'{}'}", 'session.launch_spec.files = security.compiled.files\n                session.launch_spec.digest = security.compiled.digest')
    replace_once("session.launch_spec.inputs = {'provider':'codex'}", 'session.launch_spec.inputs = security.inputs.as_dict()')
    start = source.index('        state, = factory.active.values()\n')
    end = source.index("    finally:\n        phase('cleanup start')", start)
    source = source[:start] + '''        state, = factory.active.values()
        try:
            await security.exercise(c, state, launch, manager, config)
        except Exception:
            import traceback
            traceback.print_exc()
            raise
        finally:
            # Native SessionEnd hooks still need the current main authority.
            await asyncio.wait_for(state.close(), 10)
        proof_path = Path('/evidence/result.json')
        proof = json.loads(proof_path.read_text())
        if security.provider == 'claude':
            assert state.owners.close_complete
            proof['productionEmptyTreeStopped'] = True
        else:
            assert not state.owner.alive
            proof['productionOwnerStopped'] = True
        proof_path.write_text(json.dumps(proof))
''' + source[end:]
    replace_once("        phase('API server closed')\n", "        phase('API server closed')\n        await security.close()\n")
    if owned_view or scenario.startswith('stock_race_') or scenario in ('selector','stock_decoder','stock_decoder_concurrent','stock_withdrawal','stock_public','stock_public_cancel','stock_public_failed','stock_public_lost_before','stock_public_lost_after','stock_post_chat','stock_post_expiry','stock_post_cancel','stock_post_revoke','stock_lifecycle_chat','stock_lifecycle_expiry','stock_lifecycle_revoke','stock_cancel_lost_before','stock_cancel_lost_after','stock_concurrent_native_first','stock_concurrent_chat_first','stock_concurrent_revoked_native'):
        replace_once('RuntimeTransportFactory(config, owned_view_factory=None)', 'RuntimeTransportFactory(config)')
    if scenario.startswith('stock_race_'):
        replace_once("providers={'codex': str(executable)}", "providers={'codex': str(executable),'claude':'/usr/bin/claude'}")
    if provider == 'claude':
        replace_once('    factory = RuntimeTransportFactory(config, owned_view_factory=None)', '''    from acc.broker.relay_ownership import ExclusiveRuntime
    original_launch = ExclusiveRuntime.launch
    async def diagnosed_launch(runtime):
        try:
            return await original_launch(runtime)
        except Exception:
            import traceback
            traceback.print_exc()
            print('Fixture native switches:', [x for x in runtime.spec.spec['terminal_resume_args'] if x.startswith('--')], flush=True)
            raise
    ExclusiveRuntime.launch = diagnosed_launch
    factory = RuntimeTransportFactory(config, owned_view_factory=None)''')
        replace_once('from acc.broker.relay_runtime import CLI_DIGEST, RuntimeTransportFactory',
                     'from acc.broker.relay_runtime import RuntimeTransportFactory\nfrom acc.broker.claude_owner import CLI_DIGEST')
        replace_once("Path('/usr/bin/codex').resolve()", "Path('/usr/bin/claude').resolve()")
        replace_once("providers={'codex': str(executable)}", "providers={'claude': str(executable)}")
        replace_once('launch = manager.new_launch(c.resolve)', 'launch = manager.new_exclusive_launch(c.resolve)')
        replace_once('session.id, session.run_as = SID, account', "session.id, session.run_as = SID, account\n                session.provider, session.claude_auth = 'claude', 'api_key'\n                session.native_id = '11111111-1111-4111-8111-111111111111'\n                session.launch_spec.provider = 'claude'")
        if scenario in ('handover','frozen_native','tui_expiry','tui_deny_ack','tui_deny_ack_lost','tui_allow_inventory','tui_allow_ack','tui_allow_reconcile','tui_allow_late_completion','tui_allow_execution_prepared','tui_allow_execution_sent','tui_allow_execution_completion','tui_allow_execution_lost_ack','tui_allow_execution_loss_before','tui_allow_execution_loss_after','tui_allow_execution_revoke_before','tui_allow_execution_revoke_after','tui_allow_execution_interrupt','tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new','tui_allow_restart','tui_allow_failed','tui_native_prompt','tui_allow_read','tui_allow_write','tui_allow_edit','tui_read_concurrent','tui_allow_parallel','tui_allow_ambiguous','tui_write_deny','tui_edit_deny','public_controls','roundtrip','repeat_activation','public_decisions','decision_wire','failed_target','failed_target_post','retained_draft','failed_sdk_target','failed_sdk_connect','retained_public','explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned','public_l1','claude_ack','claude_ack_allow','claude_ack_interrupt','claude_ack_lost_before','claude_ack_lost_after','claude_ack_read_allow','claude_ack_read_deny','claude_ack_read_interrupt','claude_ack_read_lost_before','claude_ack_read_lost_after','claude_ack_write_allow','claude_ack_write_deny','claude_ack_write_interrupt','claude_ack_write_lost_before','claude_ack_write_lost_after','claude_ack_edit_allow','claude_ack_edit_deny','claude_ack_edit_interrupt','claude_ack_edit_lost_before','claude_ack_edit_lost_after','claude_unoffered_glob','claude_unoffered_grep','claude_ack_read_failed','claude_ack_read_failed_lost','claude_ack_write_failed','claude_ack_write_failed_lost','claude_ack_edit_failed','claude_ack_edit_failed_lost'):
            replace_once("session.launch_spec.provider = 'claude'",
                         "session.launch_spec.provider = 'claude'\n                session.tmux_name = 'acc-offline-relay'")
    execution_function_hash=None
    if scenario.startswith('tui_allow_execution_'):
        builder_spec=importlib.util.spec_from_file_location('execution_fixture_builder',Path(__file__).with_name('execution_crash_driver.py'))
        builder=importlib.util.module_from_spec(builder_spec);builder_spec.loader.exec_module(builder)
        pinned=subprocess.check_output(['git','show','141ece9734e7f743cf60b4bb932ef600425fdc15:scripts/session_relay_qualification/lifetime_fixture.py'],cwd=root,stdin=subprocess.DEVNULL,timeout=15).decode()
        source,execution_function_hash=builder.build(pinned)
    with tempfile.TemporaryDirectory(prefix='relay-installed-security-') as name:
        output = Path(name)
        output.chmod(0o700)
        version = isolation.run(output, ['/usr/bin/' + provider, '--version'])
        expected = isolation.CODEX_VERSION if provider == 'codex' else '2.1.308 (Claude Code)'
        if version != expected:
            raise isolation.QualificationPinMismatch('Installed security CLI differs from pin')
        install = output / 'install'
        install.mkdir(mode=0o700)
        for original, target in [('bin/acc-guard','acc-guard'), ('bin/acc-hook','native-hook'),
                                 ('src/acc/env.py','acc_env.py'), ('deploy/lib/acc-env.sh','acc-env.sh')]:
            path = install / target
            path.write_bytes((root / original).read_bytes())
            path.chmod(0o500 if target in ('acc-guard','native-hook') else 0o400)
        (output / 'driver.py').write_text(source)
        if scenario=='tui_allow_restart':
            pinned=subprocess.check_output(['git','show','141ece9734e7f743cf60b4bb932ef600425fdc15:scripts/session_relay_qualification/lifetime_fixture.py'],cwd=root,stdin=subprocess.DEVNULL,timeout=15)
            (output / 'g56_lifetime_fixture.py').write_bytes(pinned)
            (output / 'native_restart_controller.py').write_bytes(Path(__file__).with_name('native_restart_controller.py').read_bytes())
        security_source = Path(__file__).with_name('installed_security.py').read_text()
        if scenario in ('handover','frozen_native','tui_expiry','tui_deny_ack','tui_deny_ack_lost','tui_allow_inventory','tui_allow_ack','tui_allow_reconcile','tui_allow_late_completion','tui_allow_execution_prepared','tui_allow_execution_sent','tui_allow_execution_completion','tui_allow_execution_lost_ack','tui_allow_execution_loss_before','tui_allow_execution_loss_after','tui_allow_execution_revoke_before','tui_allow_execution_revoke_after','tui_allow_execution_interrupt','tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new','tui_allow_restart','tui_allow_failed','tui_native_prompt','tui_allow_read','tui_allow_write','tui_allow_edit','tui_read_concurrent','tui_allow_parallel','tui_allow_ambiguous','tui_write_deny','tui_edit_deny','public_controls','roundtrip','repeat_activation','public_decisions','decision_wire','failed_target','failed_target_post','retained_draft','failed_sdk_target','failed_sdk_connect','retained_public','explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned','public_l1','claude_ack','claude_ack_allow','claude_ack_interrupt','claude_ack_lost_before','claude_ack_lost_after','claude_ack_read_allow','claude_ack_read_deny','claude_ack_read_interrupt','claude_ack_read_lost_before','claude_ack_read_lost_after','claude_ack_write_allow','claude_ack_write_deny','claude_ack_write_interrupt','claude_ack_write_lost_before','claude_ack_write_lost_after','claude_ack_edit_allow','claude_ack_edit_deny','claude_ack_edit_interrupt','claude_ack_edit_lost_before','claude_ack_edit_lost_after','claude_unoffered_glob','claude_unoffered_grep','claude_ack_read_failed','claude_ack_read_failed_lost','claude_ack_write_failed','claude_ack_write_failed_lost','claude_ack_edit_failed','claude_ack_edit_failed_lost'):
            # Generated native account under namespace tmp, never host settings.
            security_source = security_source.replace('/home/probe', '/tmp/enrolled/home')
        (output / 'installed_security.py').write_text(security_source)
        if scenario.startswith(('codex_ack', 'claude_ack')):
            (output / 'public_native_http.py').write_bytes(Path(__file__).with_name('public_native_http.py').read_bytes())
        if scenario in ('frozen_native','tui_expiry','tui_deny_ack','tui_deny_ack_lost','tui_allow_inventory','tui_allow_ack','tui_allow_reconcile','tui_allow_late_completion','tui_allow_execution_prepared','tui_allow_execution_sent','tui_allow_execution_completion','tui_allow_execution_lost_ack','tui_allow_execution_loss_before','tui_allow_execution_loss_after','tui_allow_execution_revoke_before','tui_allow_execution_revoke_after','tui_allow_execution_interrupt','tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new','tui_allow_restart','tui_allow_failed','tui_native_prompt','tui_allow_read','tui_allow_write','tui_allow_edit','tui_read_concurrent','tui_allow_parallel','tui_allow_ambiguous','tui_write_deny','tui_edit_deny','public_controls','roundtrip','repeat_activation','public_decisions','decision_wire','failed_target','failed_target_post','retained_draft','failed_sdk_target','failed_sdk_connect','retained_public','explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned','public_l1','claude_ack','claude_ack_allow','claude_ack_interrupt','claude_ack_lost_before','claude_ack_lost_after','claude_ack_read_allow','claude_ack_read_deny','claude_ack_read_interrupt','claude_ack_read_lost_before','claude_ack_read_lost_after','claude_ack_write_allow','claude_ack_write_deny','claude_ack_write_interrupt','claude_ack_write_lost_before','claude_ack_write_lost_after','claude_ack_edit_allow','claude_ack_edit_deny','claude_ack_edit_interrupt','claude_ack_edit_lost_before','claude_ack_edit_lost_after','claude_unoffered_glob','claude_unoffered_grep','claude_ack_read_failed','claude_ack_read_failed_lost','claude_ack_write_failed','claude_ack_write_failed_lost','claude_ack_edit_failed','claude_ack_edit_failed_lost'):
            (output / 'enrolled_handover.py').write_bytes(Path(__file__).with_name('enrolled_handover.py').read_bytes())
        if scenario in ('tui_deny_ack','tui_deny_ack_lost','tui_allow_inventory','tui_allow_ack','tui_allow_reconcile','tui_allow_late_completion','tui_allow_execution_prepared','tui_allow_execution_sent','tui_allow_execution_completion','tui_allow_execution_lost_ack','tui_allow_execution_loss_before','tui_allow_execution_loss_after','tui_allow_execution_revoke_before','tui_allow_execution_revoke_after','tui_allow_execution_interrupt','tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new','tui_allow_restart','tui_allow_failed','tui_native_prompt','tui_allow_read','tui_allow_write','tui_allow_edit','tui_read_concurrent','tui_allow_parallel','tui_allow_ambiguous','tui_write_deny','tui_edit_deny'):
            for helper in ('enrolled_tui_file_deny.py','enrolled_tui_deny_ack.py','enrolled_public_decisions.py','enrolled_public_controls.py','enrolled_tui_expiry.py','public_native_http.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario in ('public_controls','public_decisions'):
            (output / 'public_native_http.py').write_bytes(Path(__file__).with_name('public_native_http.py').read_bytes())
        if scenario in ('tui_expiry','tui_deny_ack','tui_deny_ack_lost','tui_allow_inventory','tui_allow_ack','tui_allow_reconcile','tui_allow_late_completion','tui_allow_execution_prepared','tui_allow_execution_sent','tui_allow_execution_completion','tui_allow_execution_lost_ack','tui_allow_execution_loss_before','tui_allow_execution_loss_after','tui_allow_execution_revoke_before','tui_allow_execution_revoke_after','tui_allow_execution_interrupt','tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new','tui_allow_restart','tui_allow_failed','tui_native_prompt','tui_allow_read','tui_allow_write','tui_allow_edit','tui_read_concurrent','tui_allow_parallel','tui_allow_ambiguous','tui_write_deny','tui_edit_deny','public_controls','roundtrip','repeat_activation','public_decisions','decision_wire','failed_target','failed_target_post','retained_draft','failed_sdk_target','failed_sdk_connect','retained_public','explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned','public_l1','claude_ack','claude_ack_allow','claude_ack_interrupt','claude_ack_lost_before','claude_ack_lost_after','claude_ack_read_allow','claude_ack_read_deny','claude_ack_read_interrupt','claude_ack_read_lost_before','claude_ack_read_lost_after','claude_ack_write_allow','claude_ack_write_deny','claude_ack_write_interrupt','claude_ack_write_lost_before','claude_ack_write_lost_after','claude_ack_edit_allow','claude_ack_edit_deny','claude_ack_edit_interrupt','claude_ack_edit_lost_before','claude_ack_edit_lost_after','claude_unoffered_glob','claude_unoffered_grep','claude_ack_read_failed','claude_ack_read_failed_lost','claude_ack_write_failed','claude_ack_write_failed_lost','claude_ack_edit_failed','claude_ack_edit_failed_lost'):
            (output / 'enrolled_frozen_native.py').write_bytes(Path(__file__).with_name('enrolled_frozen_native.py').read_bytes())
        if scenario in ('retained_public','explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned','public_l1'):
            for helper in ('enrolled_retained_draft.py','public_native_http.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario.startswith('explicit_recovery'):
            for helper in ('enrolled_failed_target.py','enrolled_failed_sdk_target.py','enrolled_explicit_recovery.py','public_native_http.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario == 'failed_sdk_connect':
            (output / 'enrolled_failed_sdk_target.py').write_bytes(Path(__file__).with_name('enrolled_failed_sdk_target.py').read_bytes())
        if scenario in ('codex_ack_interrupt','codex_ack_accept','codex_ack_lost_before','codex_ack_lost_after','codex_ack_identical','codex_ack_failed','codex_ack_failed_lost'):
            (output / 'enrolled_codex_ack.py').write_bytes(Path(__file__).with_name('enrolled_codex_ack.py').read_bytes())
        if scenario == 'codex_ack_failed_lost':
            (output / 'enrolled_codex_ack_failed.py').write_bytes(Path(__file__).with_name('enrolled_codex_ack_failed.py').read_bytes())
        if scenario == 'failed_target_post':
            (output / 'enrolled_failed_target.py').write_bytes(Path(__file__).with_name('enrolled_failed_target.py').read_bytes())
        if scenario.startswith('codex_file') and scenario!='codex_file_inventory':
            for helper in ('enrolled_codex_file_inventory.py','enrolled_codex_file_ack.py','enrolled_codex_file_remaining.py','enrolled_codex_ack.py','public_native_http.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario.startswith('claude_unoffered'):
            for helper in ('enrolled_decisions.py','enrolled_claude_unoffered.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario.startswith('claude_ack'):
            for helper in ('enrolled_decisions.py','enrolled_claude_ack.py','enrolled_claude_tool_ack.py','enrolled_claude_tool_failed.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario == 'decision_wire':
            (output / 'enrolled_decisions.py').write_bytes(Path(__file__).with_name('enrolled_decisions.py').read_bytes())
        if scenario == 'public_decisions':
            for helper in ('enrolled_tui_expiry.py','enrolled_public_controls.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario=='tui_native_prompt' or scenario.startswith('tui_allow_') and scenario!='tui_allow_inventory':
            for helper in ('enrolled_tui_allow_ack.py','enrolled_tui_allow_tools.py','enrolled_tui_file_deny.py','enrolled_tui_deny_ack.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario.startswith(('tui_allow_execution_read_','tui_allow_execution_edit_')):
            for helper in ('enrolled_tui_allow_tools.py','enrolled_tui_allow_ack.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario.endswith('_recover_new'):
            (output / 'enrolled_fresh_recovery.py').write_bytes(Path(__file__).with_name('enrolled_fresh_recovery.py').read_bytes())
        if scenario in ('tui_allow_parallel','tui_allow_ambiguous'):
            for helper in ('execution_crash_worker.py','enrolled_tui_allow_parallel.py','enrolled_tui_allow_ambiguous.py','enrolled_tui_allow_tools.py','enrolled_tui_allow_ack.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario in ('stock_race_cancel_applied','stock_race_chat_loser'):
            (output / 'enrolled_stock_settled.py').write_bytes(Path(__file__).with_name('enrolled_stock_settled.py').read_bytes())
        if scenario.startswith('stock_race_fresh_codex_'):
            for helper in ('enrolled_fresh_codex_recovery.py','enrolled_stock_fresh_codex.py','enrolled_stock_settled.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario=='stock_race_supervisor_recover_new':
            for helper in ('enrolled_stock_supervisor_loss.py','enrolled_fresh_codex_recovery.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario.startswith('stock_race_'):
            (output / 'enrolled_stock_owner_loss.py').write_bytes(Path(__file__).with_name('enrolled_stock_owner_loss.py').read_bytes())
            (output / 'enrolled_stock_race_loss.py').write_bytes(Path(__file__).with_name('enrolled_stock_race_loss.py').read_bytes())
        if scenario.startswith('stock_post_'):
            (output / 'enrolled_stock_post_delivery.py').write_bytes(Path(__file__).with_name('enrolled_stock_post_delivery.py').read_bytes())
        if scenario.startswith('stock_lifecycle_'):
            (output / 'enrolled_stock_lifecycle.py').write_bytes(Path(__file__).with_name('enrolled_stock_lifecycle.py').read_bytes())
        if scenario.startswith('stock_concurrent_'):
            (output / 'enrolled_stock_concurrent.py').write_bytes(Path(__file__).with_name('enrolled_stock_concurrent.py').read_bytes())
        if scenario.startswith(('stock_public','stock_post_','stock_lifecycle_','stock_concurrent_','stock_cancel_','stock_race_')):
            for helper in ('enrolled_stock_public.py','enrolled_codex_ack.py','public_native_http.py'):
                (output / helper).write_bytes(Path(__file__).with_name(helper).read_bytes())
        if scenario in ('stock_decoder_concurrent','stock_withdrawal'):
            (output / 'enrolled_stock_decoder.py').write_bytes(Path(__file__).with_name('enrolled_stock_decoder.py').read_bytes())
        if scenario != 'security':
            (output / f'enrolled_{scenario}.py').write_bytes(Path(__file__).with_name(f'enrolled_{scenario}.py').read_bytes())
        (output / 'relay_helpers.py').write_bytes((root / 'tests/relay_helpers.py').read_bytes())
        (output / 'mode.json').write_text(json.dumps('node-partition'))
        (output / 'security.json').write_text(json.dumps({'provider':provider,'tampered':tampered,'owned_view':owned_view,**({'executionStage':scenario.removeprefix('tui_allow_execution_').removeprefix('read_').removeprefix('edit_').removeprefix('failed_').removesuffix('_recover_new').replace('policy_','revoke_').replace('strict_','revoke_').replace('ownership_','revoke_').replace('account_','revoke_').replace('workspace_','revoke_'), 'executionRevocation':next((kind for kind in ('policy','strict','ownership','account','workspace') if '_'+kind+'_' in scenario),'cookie'),'executionReadyWindow':scenario=='tui_allow_execution_ownership_before','executionBindingLoss':any('_'+kind+'_' in scenario for kind in ('account','workspace')),'executionFailure':'_failed_' in scenario,'executionRecoveryNew':scenario.endswith('_recover_new'), 'executionTool':'Read' if '_read_' in scenario else 'Edit' if '_edit_' in scenario else 'Bash'} if execution_function_hash else {})}))
        account_home = '/tmp/enrolled/home' if scenario in ('handover','frozen_native','tui_expiry','tui_deny_ack','tui_deny_ack_lost','tui_allow_inventory','tui_allow_ack','tui_allow_reconcile','tui_allow_late_completion','tui_allow_execution_prepared','tui_allow_execution_sent','tui_allow_execution_completion','tui_allow_execution_lost_ack','tui_allow_execution_loss_before','tui_allow_execution_loss_after','tui_allow_execution_revoke_before','tui_allow_execution_revoke_after','tui_allow_execution_interrupt','tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new','tui_allow_restart','tui_allow_failed','tui_native_prompt','tui_allow_read','tui_allow_write','tui_allow_edit','tui_read_concurrent','tui_allow_parallel','tui_allow_ambiguous','tui_write_deny','tui_edit_deny','public_controls','roundtrip','repeat_activation','public_decisions','decision_wire','failed_target','failed_target_post','retained_draft','failed_sdk_target','failed_sdk_connect','retained_public','explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned','public_l1','claude_ack','claude_ack_allow','claude_ack_interrupt','claude_ack_lost_before','claude_ack_read_allow','claude_ack_read_deny','claude_ack_read_interrupt','claude_ack_read_lost_before','claude_ack_read_lost_after','claude_ack_write_allow','claude_ack_write_deny','claude_ack_write_interrupt','claude_ack_write_lost_before','claude_ack_write_lost_after','claude_ack_edit_allow','claude_ack_edit_deny','claude_ack_edit_interrupt','claude_ack_edit_lost_before','claude_ack_edit_lost_after','claude_ack_lost_after') else '/home/probe'
        if scenario.startswith('tui_deny_ack') or scenario.startswith(('claude_ack','claude_unoffered')):
            account_home='/tmp/enrolled/home'
        (output / 'passwd').write_text(f'relayagent:x:{os.getuid()}:{os.getgid()}:Offline fixture:{account_home}:/bin/sh\n')
        (output / 'group').write_text(f'relayagent:x:{os.getgid()}:\n')
        command = isolation.sandbox(output)[:-1] + [
            '--ro-bind', str(root / 'src'), '/fixture/src', '--ro-bind', str(dependencies), '/deps',
            '--dir', '/etc', '--ro-bind', str(output / 'passwd'), '/etc/passwd',
            '--ro-bind', str(output / 'group'), '/etc/group',
            '--dir', '/opt', '--ro-bind', str(install), '/opt/relay-install',
            '--setenv', 'PYTHONPATH', '/fixture/src:/deps:/evidence', '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
        ]
        if scenario == 'selector':
            command += ['--bind', str(bridge), '/evidence/bridge']
        if scenario in ('handover','frozen_native','tui_expiry','tui_deny_ack','tui_deny_ack_lost','tui_allow_inventory','tui_allow_ack','tui_allow_reconcile','tui_allow_late_completion','tui_allow_execution_prepared','tui_allow_execution_sent','tui_allow_execution_completion','tui_allow_execution_lost_ack','tui_allow_execution_loss_before','tui_allow_execution_loss_after','tui_allow_execution_revoke_before','tui_allow_execution_revoke_after','tui_allow_execution_interrupt','tui_allow_execution_read_loss_before','tui_allow_execution_read_loss_after','tui_allow_execution_edit_loss_before','tui_allow_execution_edit_loss_after','tui_allow_execution_read_failed_loss_after','tui_allow_execution_edit_failed_loss_after','tui_allow_execution_interrupt_recover_new','tui_allow_execution_policy_before','tui_allow_execution_policy_after','tui_allow_execution_interrupt_before','tui_allow_execution_strict_before','tui_allow_execution_strict_after','tui_allow_execution_ownership_before','tui_allow_execution_ownership_after','tui_allow_execution_account_before','tui_allow_execution_account_after','tui_allow_execution_workspace_before','tui_allow_execution_workspace_after','tui_allow_execution_loss_before_recover_new','tui_allow_execution_loss_after_recover_new','tui_allow_restart','tui_allow_failed','tui_native_prompt','tui_allow_read','tui_allow_write','tui_allow_edit','tui_read_concurrent','tui_allow_parallel','tui_allow_ambiguous','tui_write_deny','tui_edit_deny','public_controls','roundtrip','repeat_activation','public_decisions','decision_wire','failed_target','failed_target_post','retained_draft','failed_sdk_target','failed_sdk_connect','retained_public','explicit_recovery','explicit_recovery_pre','explicit_recovery_sdk','explicit_recovery_sdk_connect','explicit_recovery_terminal','explicit_recovery_lost_owned','public_l1','claude_ack','claude_ack_allow','claude_ack_interrupt','claude_ack_lost_before','claude_ack_lost_after','claude_ack_read_allow','claude_ack_read_deny','claude_ack_read_interrupt','claude_ack_read_lost_before','claude_ack_read_lost_after','claude_ack_write_allow','claude_ack_write_deny','claude_ack_write_interrupt','claude_ack_write_lost_before','claude_ack_write_lost_after','claude_ack_edit_allow','claude_ack_edit_deny','claude_ack_edit_interrupt','claude_ack_edit_lost_before','claude_ack_edit_lost_after','claude_unoffered_glob','claude_unoffered_grep','claude_ack_read_failed','claude_ack_read_failed_lost','claude_ack_write_failed','claude_ack_write_failed_lost','claude_ack_edit_failed','claude_ack_edit_failed_lost'):
            command += ['--setenv','HOME',account_home,'--setenv','CLAUDE_CONFIG_DIR',account_home+'/.claude']
        command += ['--', '/usr/bin/python3', '/evidence/driver.py']
        result = subprocess.run(command, env={'PATH':'/usr/bin:/bin'}, capture_output=True,
            timeout=380 if scenario=='decision_expiry' else 160 if scenario.startswith('tui_allow_execution_') else 140 if scenario=='tui_allow_late_completion' else 100)
        if result.returncode:
            diagnostic=output/'execution-progress.jsonl'
            progress=diagnostic.read_text()[-12000:] if diagnostic.exists() else ''
            faults=output/'execution-faults.log'
            if faults.exists():progress+='\nWorker stacks:\n'+faults.read_text()[-20000:]
            raise RuntimeError('Offline installed security proof failed: ' + result.stderr.decode(errors='replace')[-16000:]
                               + result.stdout.decode(errors='replace')[-2000:] + '\nExecution progress:\n' + progress)
        proof = json.loads((output / 'result.json').read_text())
        if execution_function_hash:
            proof['g56ControllerFunctionSha256']=execution_function_hash
            proof['executionProgress']=[json.loads(line) for line in (output/'execution-progress.jsonl').read_text().splitlines()]
        proof['cli_version'] = version
        proof['installed_source_sha256'] = {target:hashlib.sha256((install / target).read_bytes()).hexdigest()
                                            for target in ('acc-guard','native-hook','acc_env.py','acc-env.sh')}
        return proof


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Fixed offline selector bridge; no native command or environment input.')
    parser.add_argument('--bridge', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    args = parser.parse_args()
    print(json.dumps(capture_installed_security(scenario='selector', bridge=args.bridge, session_id=args.session_id)))
