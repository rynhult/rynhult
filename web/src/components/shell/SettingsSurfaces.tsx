/** Opt in Settings' section wrappers without restyling shared widgets on other routes. */
import { createContext, useContext, type ComponentProps } from 'react';
import * as Legacy from '../ui';
import { Surface, SurfaceHeader } from './PagePrimitives';

const SettingsScope = createContext(false);
export const SettingsSurfaceScope = SettingsScope.Provider;
export function SettingsCard(props: ComponentProps<typeof Legacy.Card>) {
  const scoped = useContext(SettingsScope);
  return scoped ? <Surface {...props} /> : <Legacy.Card {...props} />;
}
export function SettingsCardHeader(props: ComponentProps<typeof Legacy.CardHeader>) {
  const scoped = useContext(SettingsScope);
  return scoped ? <SurfaceHeader {...props} /> : <Legacy.CardHeader {...props} />;
}
