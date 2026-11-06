import { expect, test } from './fixtures';
import { NAV, navFor } from '../src/components/nav';
import { modeForPath, railFor, toolsFor } from '../src/components/shell/navigation';
import { localKey, projectIds, resolveProjects } from '../src/components/shell/presentation';

test('shell destinations cover existing filtered navigation without owner leakage', () => {
  for (const owner of [false, true]) for (const groups of [false, true]) {
    const features = { publishing: false, schedules: false, groups: true };
    const rail = railFor(owner, groups, features);
    const paths = [...rail.map(x => x.to), ...toolsFor(owner, groups, features).map(x => x.to)];
    for (const n of navFor(owner, groups, features).flatMap(g => g.items)) expect(paths).toContain(n.to === '/host' ? '/hosts' : n.to);
    if (!owner) { expect(paths).not.toContain('/board'); expect(paths).not.toContain('/audit'); }
    expect(paths).not.toContain('/publishing');
  }
  expect(railFor(true, false, {}, () => false)).toEqual([]);
});

test('presentation keys separate installations and people; ids resolve only through authorized rows', () => {
  expect(localKey('https://demo.example.lan', 'person-one', 'pins')).not.toBe(localKey('https://demo.example.lan', 'person-two', 'pins'));
  expect(localKey('https://demo.example.lan', 'person-one', 'pins')).not.toBe(localKey('https://other.example.lan', 'person-one', 'pins'));
  expect(localKey('https://demo.example.lan', null, 'pins')).toBeNull();
  expect(projectIds([1, 'private name', 2, 1, -1, null])).toEqual([1, 2]);
  expect(resolveProjects([1, 2, 3], [{ id: 2, name: 'Authorized' }])).toEqual([{ id: 2, name: 'Authorized' }]);
});

test('route selection covers every authorized destination and nested route without prefix collisions', () => {
  for (const owner of [false, true]) for (const groupAdmin of [false, true]) {
    const features = { publishing: true, schedules: true, groups: true };
    const rail = railFor(owner, groupAdmin, features), tools = toolsFor(owner, groupAdmin, features);
    for (const item of rail) {
      expect(modeForPath(item.to, rail, tools)).toBe(item.id);
      if (item.to !== '/') expect(modeForPath(`${item.to}/detail`, rail, tools)).toBe(item.id);
    }
    for (const item of tools) {
      expect(modeForPath(item.to, rail, tools)).toBe('tools');
      expect(modeForPath(`${item.to}/detail`, rail, tools)).toBe('tools');
    }
    expect(modeForPath('/host', rail, tools)).toBe('hosts');
    expect(modeForPath('/multiruns/batch-one', rail, tools)).toBe('sessions');
    for (const path of ['/projectsmith', '/sessions-other', '/providers-private', '/unknown']) expect(modeForPath(path, rail, tools)).toBeUndefined();
    if (!owner) for (const path of ['/audit', '/board', '/audit/1']) expect(modeForPath(path, rail, tools)).toBeUndefined();
    if (!owner && !groupAdmin) expect(modeForPath('/groups/1', rail, tools)).toBeUndefined();
  }
  const disabled = { publishing: false, schedules: false, groups: false };
  for (const path of ['/publishing', '/schedules', '/groups']) expect(modeForPath(path, railFor(true, false, disabled), toolsFor(true, false, disabled))).toBeUndefined();
});

test('Tools uses the first allowed tool when Catalog is hidden by the desktop navigation', () => {
  const saved = NAV.map(group => group.items);
  const desktopPaths = ['/inbox', '/sessions', '/projects', '/search', '/providers', '/audit', '/guide'];
  try {
    for (const group of NAV) group.items = group.items.filter(item => desktopPaths.includes(item.to));
    for (const owner of [false, true]) {
      const rail = railFor(owner);
      expect(rail.find(item => item.id === 'tools')?.to).toBe('/search');
      expect(modeForPath('/providers', rail, toolsFor(owner))).toBe('tools');
      expect([...rail, ...toolsFor(owner)].some(item => item.to === '/catalog')).toBe(false);
      if (!owner) expect(modeForPath('/audit', rail, toolsFor(owner))).toBeUndefined();
    }
  } finally { NAV.forEach((group, index) => { group.items = saved[index]; }); }
});
