"""Test-only WebKitGTK layout probe, on a fixture-owned Broadway display."""
import json
import sys

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
gi.require_version('Soup', '3.0')
from gi.repository import GLib, Gtk, Soup, WebKit2  # noqa: E402

config = json.load(sys.stdin)
Gtk.Settings.get_default().set_property('gtk-xft-dpi', 96 * 1024)
window = Gtk.Window()
window.set_default_size(1440, 900)
view = WebKit2.WebView()
window.add(view)
window.show_all()
loop = GLib.MainLoop()


def done(view, result, data):
    try:
        print(view.evaluate_javascript_finish(result).to_string(), flush=True)
    finally:
        loop.quit()


def inspect():
    view.evaluate_javascript('''JSON.stringify({zoom:devicePixelRatio, viewport:innerWidth,
    elements: [...document.querySelectorAll('html,body,main,main>div,main>div>header,main h1')].map(e => {
      const s=getComputedStyle(e), r=e.getBoundingClientRect();
      return {tag:e.tagName,cls:e.className,w:r.width,x:r.x,font:s.fontSize, width:s.width,max:s.maxWidth,display:s.display};
    })})''', -1, None, None, None, done, None)
    return False


def loaded(view, event):
    if event == WebKit2.LoadEvent.FINISHED:
        GLib.timeout_add(3000, inspect)


view.connect('load-changed', loaded)
manager = view.get_context().get_cookie_manager()


def cookie_done(manager, result, data):
    manager.add_cookie_finish(result)
    view.load_uri(config['origin'])


cookie = Soup.Cookie.new('rynhult_desktop', config['token'], '127.0.0.1', '/', -1)
manager.add_cookie(cookie, None, cookie_done, None)
GLib.timeout_add(20000, lambda: loop.quit())
loop.run()
