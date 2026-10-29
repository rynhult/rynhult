"""Bounded inert document readers, executed only in an account-broker child.

No office automation, links, macros, embedded objects or external XML entities.
The child installs a fail-closed Linux seccomp network filter before parsing.
"""
from __future__ import annotations

import base64
import ctypes
import errno
import io
import json
import os
import platform
import re
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

MAX_INPUT = 10 << 20
MAX_TEXT = 200000
MAX_XML = 20 << 20
DEADLINE = 15


def deny_network():
    """Inherited by Ghostscript too. Unknown machines refuse extraction."""
    machine = platform.machine()
    if machine == 'x86_64':
        architecture, calls = 0xc000003e, [41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 288]
    elif machine == 'aarch64':
        architecture, calls = 0xc00000b7, list(range(198, 213)) + [242]
    else:
        raise ValueError('Network isolation is unavailable on this machine.')

    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte), ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]

    # Kill foreign syscall ABIs, including x32 (whose syscall number has bit 30).
    rules = [(0x20, 0, 0, 4), (0x15, 1, 0, architecture), (0x06, 0, 0, 0x80000000),
             (0x20, 0, 0, 0), (0x45, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    calls += [425, 426, 427]  # io_uring can otherwise create/connect sockets without socket syscalls.
    for call in calls:
        rules += [(0x15, 0, 1, call), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    rules += [(0x06, 0, 0, 0x7fff0000)]
    filters = (Filter * len(rules))(*(Filter(*r) for r in rules))
    program = Program(len(rules), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.byref(program), 0, 0):
        raise ValueError('Network isolation is unavailable; file kept as a reference.')


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'template'):
            self.hidden += 1
        elif not self.hidden and tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'template') and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def rtf_text(value):
    if not value.startswith('{\\rtf'):
        raise ValueError('Malformed RTF. Upload plain text instead.')
    out, stack, skip, uc, fallback = [], [], False, 1, 0
    token = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|[{}]|[^\\{}]+")
    for match in token.finditer(value):
        raw, word, number, hexchar, symbol = match[0], *match.groups()
        if raw == '{':
            stack.append((skip, uc))
            if len(stack) > 256:
                raise ValueError('RTF nesting exceeds the limit.')
        elif raw == '}':
            if not stack:
                raise ValueError('Malformed RTF groups.')
            skip, uc = stack.pop()
        elif word:
            if word in ('object', 'objdata', 'pict', 'fonttbl', 'colortbl', 'stylesheet', 'info', 'filetbl'):
                skip = True
            elif word == 'bin':
                raise ValueError('Binary RTF is unsupported. Upload plain text instead.')
            elif word == 'uc':
                uc = min(16, max(0, int(number or '1')))
            elif word == 'u' and not skip:
                out.append(chr(int(number or '0') % 65536)); fallback = uc
            elif word in ('par', 'line', 'tab') and not skip:
                out.append('\n' if word != 'tab' else '\t')
        elif symbol == '*':
            skip = True
        elif not skip:
            part = bytes.fromhex(hexchar).decode('cp1252', errors='replace') if hexchar else symbol or raw
            take = min(fallback, len(part)); fallback -= take
            out.append(part[take:])
    if stack:
        raise ValueError('Malformed RTF groups.')
    return ''.join(out)


def extract(data, extension):
    if not data or len(data) > MAX_INPUT:
        raise ValueError('Choose a nonempty file of at most 10 MiB.')
    if extension in ('png', 'jpg', 'webp'):
        raise ValueError('Image requires visual reading; file kept as a reference. Supply a text description if unreadable.')
    pages = None
    if extension in ('docx', 'odt'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 512 or sum(e.file_size for e in entries) > MAX_XML or any(
                        e.flag_bits & 1 or e.file_size > MAX_XML or e.file_size > max(1, e.compress_size) * 200
                        for e in entries):
                    raise ValueError('Office archive exceeds expansion limits.')
                member = 'word/document.xml' if extension == 'docx' else 'content.xml'
                if sum(e.filename == member for e in entries) != 1:
                    raise ValueError('Missing or duplicate document body.')
                xml = archive.read(member)
            # Decode before checking so UTF-16 cannot hide a DTD/entity declaration.
            if b'\x00' in xml or re.search(br'<!\s*(DOCTYPE|ENTITY)', xml, re.I):
                raise ValueError('XML entities and DTDs are not accepted.')
            root = ElementTree.fromstring(xml)
            value = '\n'.join(''.join(p.itertext()) for p in root.iter() if p.tag.rsplit('}', 1)[-1] in ('p', 'h'))
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError) as error:
            raise ValueError('Malformed office document. Upload plain text instead.') from error
    elif extension == 'pdf':
        if not data.startswith(b'%PDF-') or not shutil.which('gs'):
            raise ValueError('PDF text extraction needs installed Ghostscript; file kept as a reference.')
        with tempfile.TemporaryDirectory(prefix='brief-pdf-') as folder:
            source, target = Path(folder) / 'input.pdf', Path(folder) / 'text.txt'
            source.write_bytes(data)
            try:
                result = subprocess.run(['gs', '-q', '-dSAFER', '-dBATCH', '-dNOPAUSE', '-dPDFSTOPONERROR',
                    '-sDEVICE=txtwrite', '-dTextFormat=0', '-dFirstPage=1', '-dLastPage=200', f'-sOutputFile={target}', str(source)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            except subprocess.TimeoutExpired as error:
                raise ValueError('PDF extraction timed out; file kept as a reference.') from error
            if result.returncode or not target.exists() or target.stat().st_size > 2 << 20:
                raise ValueError('PDF extraction failed or exceeded its output limit.')
            value = target.read_text(errors='replace')
            try:
                root = ElementTree.fromstring('<document>' + value + '</document>')
                pages = ['\n'.join(''.join(c.get('c', '') for c in span.iter('char'))
                                    for span in page.iter('span')) for page in root.findall('page')]
                value = '\n'.join(pages)
            except ElementTree.ParseError as error:
                raise ValueError('PDF text could not be decoded; file kept as a reference.') from error
    else:
        try:
            value = data.decode('utf-8-sig')
        except UnicodeError as error:
            raise ValueError('Text must be UTF-8. Save as UTF-8 and upload again.') from error
        if '\0' in value:
            raise ValueError('Text contains binary data.')
        if extension == 'html':
            parser = HTMLText(); parser.feed(value); value = ''.join(parser.parts)
        elif extension == 'rtf':
            value = rtf_text(value)
        elif extension not in ('md', 'txt'):
            raise ValueError('This format needs visual reading; file kept as a reference.')
    sections, remaining = [], MAX_TEXT
    for n, part in enumerate(pages if pages is not None else [value], 1):
        if not remaining:
            break
        if part.strip():
            sections.append({'location': f'page {n}' if pages is not None else 'section 1', 'text': part[:remaining]})
            remaining -= len(part[:remaining])
    if not sections:
        raise ValueError('No readable text found; file kept as a reference. Supply a text description.')
    return {'sections': sections, 'truncated': len(value) > MAX_TEXT or extension == 'pdf', 'untrusted_evidence': True}


def run(config, extension, data):
    """Fixed broker operation, never an arbitrary path/command from an agent."""
    if extension not in ('md', 'txt', 'pdf', 'docx', 'odt', 'rtf', 'html', 'png', 'jpg', 'webp'):
        raise ValueError('Unsupported document format.')
    if not isinstance(data, str) or len(data) > 4 * -(-MAX_INPUT // 3):
        raise ValueError('Document exceeds 10 MiB.')
    raw = base64.b64decode(data, validate=True)
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError('Choose a nonempty file of at most 10 MiB.')
    # Input is a private temporary file, not a path accd reads in an agent checkout.
    with tempfile.TemporaryDirectory(prefix='brief-extract-') as folder:
        os.chmod(folder, 0o700)
        source = Path(folder) / 'source'
        source.write_bytes(raw); source.chmod(0o600)
        process = subprocess.Popen([sys.executable, '-I', '-S', str(Path(__file__).resolve()), extension, str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=folder, env={'PATH': '/usr/bin:/bin', 'TMPDIR': folder}, start_new_session=True)
        output, deadline = bytearray(), time.monotonic() + DEADLINE
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise ValueError('Extraction timed out; file kept as a reference.')
                    for entry, _ in selector.select(.05):
                        chunk = os.read(entry.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(entry.fileobj)
                        output.extend(chunk)
                        if len(output) > 2 << 20:
                            raise ValueError('Extraction output exceeded the limit.')
            process.wait(timeout=max(.1, deadline - time.monotonic()))
            if process.returncode:
                raise ValueError('Extraction failed or exceeded resource limits; file kept as a reference.')
            return json.loads(output)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(); process.stdout.close()


if __name__ == '__main__':
    try:
        resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 << 20, 2 << 20))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        deny_network()
        result = extract(Path(sys.argv[2]).read_bytes(), sys.argv[1])
        print(json.dumps({'status': 'ready', **result}))
    except Exception as error:
        print(json.dumps({'status': 'error', 'error': str(error)[:500], 'untrusted_evidence': True}))
