import type { FullConfig, FullProject } from '@playwright/test';

type Project = Pick<FullProject, 'name' | 'metadata'>;
export function presentationProject(project: Project) {
  return project.metadata?.presentationIsolation === true ||
    ['desktop-stateful', 'mobile-stateful', 'terminal-chromium'].includes(project.name);
}

/** Selection, not all configured projects; dependencies can add another family. */
export function assertSafeWorkers(config: FullConfig) {
  const selected = new Map<string, FullProject>();
  const add = (project: FullProject) => {
    if (selected.has(project.name)) return;
    selected.set(project.name, project);
    for (const name of project.dependencies ?? []) {
      const dependency = config.projects.find(p => p.name === name);
      if (dependency) add(dependency);
    }
  };
  for (const project of config.filteredProjects) add(project);
  const families = [...selected.values()].filter(p => p.name !== 'setup');
  if (config.workers > 1 && families.length > 1 && families.some(presentationProject)) {
    throw new Error('Shared presentation projects cannot overlap other projects. Use --workers=1 for this selection, or run one family at a time.');
  }
}

export function assertCleanBoundary(changed: string[], previous: string | null, current: string) {
  if (changed.length) throw new Error(`Presentation drift before reset: ${changed.join(', ')}. Previous test: ${previous ?? 'initial state / beforeAll'}. Next test: ${current}. Check afterAll/beforeAll ownership; defaults have been restored.`);
}

/** Lint rule: shared suite modules need the fixture just as .spec.ts files do. */
export function baseTestImports(source: string, file: string): string[] {
  // Only the module defining the extension may import the base test object.
  if (file === 'fixtures.ts') return [];
  // Restrict executable static imports, including aliases and multiline clauses.
  // Type-only imports do not create an alternate test object.
  const imports = source.matchAll(/\bimport\s+(?!type\b)(\{[^}]*\}|(?:[$\w]+\s*,\s*)?\*\s+as\s+[$\w]+|[$\w]+(?:\s*,\s*\{[^}]*\})?)\s+from\s*['"]@playwright\/test['"]/g);
  for (const [, clause] of imports) {
    if (clause.trim().startsWith('*') || !clause.trim().startsWith('{')) return [file];
    const names = clause.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, '').replace(/[{}]/g, '').split(',').map(name => name.trim());
    if (names.some(name => /^test(?:\s|$)/.test(name))) return [file];
  }
  return [];
}
