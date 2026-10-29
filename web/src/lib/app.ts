/** Whether Rynhult runs as the installed app (added to the home screen), not in a browser tab. */
export const installedApp = () =>
  window.matchMedia("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;
