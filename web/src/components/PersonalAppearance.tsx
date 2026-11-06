import { Link } from 'react-router-dom';
import { useRole } from '../lib/queries';
import type { ThemeChoice } from '../lib/theme';
import { saveTheme, THEME_LABEL, useThemeChoice } from '../lib/themeChoice';
import { Card, CardHeader, Field, Select, buttonClass } from './ui';
import { AppearanceThemes } from './AppearanceThemes';

export function PersonalAppearance() {
  const { owner } = useRole();
  const choice = useThemeChoice();
  return <section aria-label="Personal appearance" className="min-w-0 space-y-4">
    <Card><CardHeader title="Appearance" subtitle="Personal presentation. Your sessions, permissions and saved layout stay the same." />
      <div className="space-y-3 p-4">
        <Field label="Appearance mode" hint="System follows your device. This mode is saved in this browser; your installed theme choice belongs to your account.">
          <Select value={choice} onChange={event => saveTheme(event.target.value as ThemeChoice)}>
            {(['system', 'light', 'dark'] as const).map(mode => <option key={mode} value={mode}>{THEME_LABEL[mode]}</option>)}
          </Select>
        </Field>
        {owner ? <Link className={buttonClass()} to="/catalog?tab=themes">Manage themes in Catalog</Link> : <p className="text-sm text-muted">An owner installs shared themes in Catalog. Preview and apply an installed theme to your own account below.</p>}
      </div>
    </Card>
    <AppearanceThemes context="account" />
  </section>;
}
