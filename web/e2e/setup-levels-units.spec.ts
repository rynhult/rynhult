import { expect, test } from './fixtures';
import { railFor, toolsFor } from '../src/components/shell/navigation';

const commonRail = ['overview', 'inbox', 'sessions', 'projects', 'hosts', 'tools', 'settings'];
const fullRail = ['overview', 'inbox', 'sessions', 'projects', 'workspaces', 'hosts', 'grid', 'tools', 'settings'];
const commonTools = ['/search', '/providers', '/guide'];
const soloTools = ['/search', '/schedules', '/publishing', '/providers', '/catalog', '/guide'];
const fullTools = ['/search', '/schedules', '/publishing', '/providers', '/board', '/catalog', '/audit', '/groups', '/guide'];
for (const level of [null, 0, 1, 2, 3, 4, 5, 6]) for (const owner of [true, false]) {
  test(`level ${level}, ${owner ? 'owner' : 'member'} sees only authorized normal areas`, async () => {
    const { modeVisible, pathVisible } = await import('../src/lib/setupLevels');
    expect(railFor(owner, false, {}, mode => modeVisible(level, mode)).map(n => n.id)).toEqual(level === 1 ? commonRail : fullRail);
    const expected = level === 1 ? commonTools : level === 2 ? soloTools : fullTools;
    expect(toolsFor(owner, false, {}, path => pathVisible(level, path)).map(n => n.to))
      .toEqual(expected.filter(p => owner || !['/board', '/audit', '/groups'].includes(p)));
    const available = toolsFor(owner, false, { publishing: false, schedules: false, groups: false }, path => pathVisible(level, path));
    expect(available.map(n => n.to)).not.toContain('/publishing');
    expect(available.map(n => n.to)).not.toContain('/schedules');
    expect(available.map(n => n.to)).not.toContain('/groups');
  });
}

test('recommendations keep every quiet protection On and helpers optional', async () => {
  const { LEVELS, SECURITY_DEFAULTS } = await import('../src/lib/setupLevels');
  expect(LEVELS.map(x => x.id)).toEqual([0, 1, 2, 3, 4, 5, 6]);
  expect(SECURITY_DEFAULTS).toEqual({ force_push: true, push_default: true, merge_pr: true, rm_outside: true,
    secrets: true, custom_rules: true, allowed_folders: true, tool_install_check: true,
    provider_prompts: true, budget_checks: true, risk_labels: true });
  expect(LEVELS[1].team).toContain('One ready agent');
  expect(LEVELS[0].team).toContain('explicit helper ceiling');
});

test('a group maintainer retains only their authorized group entry as levels change', async () => {
  const { pathVisible } = await import('../src/lib/setupLevels');
  expect(toolsFor(false, true, {}, path => pathVisible(1, path)).map(n => n.to)).not.toContain('/groups');
  const team = toolsFor(false, true, {}, path => pathVisible(3, path)).map(n => n.to);
  expect(team).toContain('/groups'); expect(team).not.toContain('/audit'); expect(team).not.toContain('/board');
});
