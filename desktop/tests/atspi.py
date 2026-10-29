"""Minimal AT-SPI inspector over raw D-Bus, for sessions without at-spi2-core.

No registry daemon exists here, so instead of asking the registry for the
desktop we visit each application's root accessible directly, as an assistive
technology would after the registry hands it over. Run with the system Python
(PyGObject) in isolated mode: python3 -I atspi.py <bus address> [--focused]
Prints one JSON document: every application's tree, or only focused objects.
"""
import json
import sys

import gi

gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, Gio, GLib  # noqa: E402  (Atspi only names roles; no registry)

ACCESSIBLE = 'org.a11y.atspi.Accessible'
ROOT = '/org/a11y/atspi/accessible/root'
# AT-SPI state bit positions (atspi-constants.h).
STATES = {'focusable': 11, 'focused': 12, 'showing': 25, 'visible': 30}


def call(bus, name, path, interface, method, args=None, reply=None):
    return bus.call_sync(name, path, interface, method, args, reply and GLib.VariantType(reply),
                         Gio.DBusCallFlags.NONE, 2000, None).unpack()


def prop(bus, name, path, interface, key):
    return call(bus, name, path, 'org.freedesktop.DBus.Properties', 'Get',
                GLib.Variant('(ss)', (interface, key)), '(v)')[0]


def node(bus, name, path, depth, seen, limit):
    if (name, path) in seen or depth > limit:
        return None
    seen.add((name, path))
    try:
        # WebKitGTK answers GetRoleName with ''; screen readers use the numeric role.
        role = Atspi.role_get_name(Atspi.Role(call(bus, name, path, ACCESSIBLE, 'GetRole', None, '(u)')[0]))
        label = prop(bus, name, path, ACCESSIBLE, 'Name')
        low, high = call(bus, name, path, ACCESSIBLE, 'GetState', None, '(au)')[0]
        children = call(bus, name, path, ACCESSIBLE, 'GetChildren', None, '(a(so))')[0]
    except GLib.Error as error:
        return {'error': error.message.split(':')[-1].strip(), 'bus': name, 'path': path}
    bits = low | high << 32
    states = sorted(key for key, bit in STATES.items() if bits >> bit & 1)
    result = {'role': role, 'name': label, 'states': states, 'bus': name, 'path': path}
    kids = [node(bus, child_bus, child_path, depth + 1, seen, limit) for child_bus, child_path in children]
    # A child can vanish mid-walk while the page re-renders; the next poll sees the new tree.
    if kids := [kid for kid in kids if kid and 'error' not in kid]:
        result['children'] = kids
    return result


def walk(address, limit=40):
    bus = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    names = call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'ListNames',
                 None, '(as)')[0]
    seen, apps = set(), []
    for name in sorted(n for n in names if n.startswith(':')):
        tree = node(bus, name, ROOT, 0, seen, limit)
        if tree and 'error' not in tree and tree['role'] == 'application':
            apps.append(tree)
    return bus, apps


def flatten(tree):
    yield tree
    for child in tree.get('children', []):
        yield from flatten(child)


def do_action(address, name, path, index=0):
    bus = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    return call(bus, name, path, 'org.a11y.atspi.Action', 'DoAction', GLib.Variant('(i)', (index,)), '(b)')[0]


if __name__ == '__main__':
    if sys.argv[2:3] == ['--action']:
        print(json.dumps(do_action(sys.argv[1], sys.argv[3], sys.argv[4])))
        sys.exit()
    _, apps = walk(sys.argv[1])
    if '--focused' in sys.argv:
        apps = [{k: v for k, v in item.items() if k != 'children'}
                for app in apps for item in flatten(app) if 'focused' in item['states']]
    print(json.dumps(apps, indent=1))
