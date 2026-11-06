import { useLayoutEffect } from 'react';
import { themeCss, trustedCss, type ThemePreference } from '../lib/community-theme';

/** Custom-theme rendering is absent from built-in/sign-in cold loads. */
export default function ThemeAppearance({ person, preference, failed, recovery }: {
  person: string; preference: ThemePreference | undefined; failed: boolean; recovery: boolean;
}) {
  let css = '', invalid = false;
  if (!failed && preference?.effective && !recovery) {
    try { css = themeCss(preference.effective); } catch { invalid = true; }
  }
  useLayoutEffect(() => {
    const node = document.createElement('style'); node.dataset.personalTheme = '';
    const updateMeta = () => {
      const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
      if (meta) meta.content = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
    };
    if (css) { node.textContent = css; document.head.append(node); document.documentElement.setAttribute('data-community-theme', ''); }
    else document.documentElement.removeAttribute('data-community-theme');
    updateMeta();
    return () => { node.remove(); document.documentElement.removeAttribute('data-community-theme'); updateMeta(); };
  }, [css, person]);
  return <><style>{trustedCss}</style>{(invalid || preference?.unavailable || failed || recovery) &&
    <div role="status" className="theme-trusted fixed bottom-16 left-3 right-3 z-50 rounded-md border border-line bg-panel p-3 text-fg shadow-pop">
      {recovery ? 'Built-in appearance recovery is active.' : 'Custom theme unavailable. Using built-in Rynhult.'} {' '}
      <a className="underline" href="/catalog?appearance=builtin">Open Catalog → Themes to reset</a>
    </div>}</>;
}
