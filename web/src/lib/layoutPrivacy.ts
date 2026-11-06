/** Device layout records end at the auth boundary, including sign-out from the standard shell after a reload.
 * Old records held relative file names; pending copies hold opaque references but are still one person's.
 */
export function clearWorkingLayouts() {
  try {
    for (const key of Object.keys(localStorage)) if (/^ryn(hult:working|-layout-pending)/.test(key)) localStorage.removeItem(key);
  } catch { /* Unavailable browser storage cannot be read or erased by this page. */ }
}
