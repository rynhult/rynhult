"""Isolated HTTP payload meter for pinned restic; synthetic data only, no cloud credentials.

Run: PYTHONPATH=src python tests/backup_cost_meter.py /usr/local/bin/restic OUTPUT.json
Counts application payload bytes, not TLS/IP overhead or a provider invoice.
"""
import collections
import hashlib
import io
import json
import random
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape


class Meter:
    def __init__(self, protocol):
        self.protocol, self.objects, self.events = protocol, {}, []

    def handler(self):
        meter = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                path = parsed.path.removeprefix('/bucket/') if meter.protocol == 's3' else parsed.path.removeprefix('/repo/')
                def chunks(stream):
                    result = bytearray()
                    while True:
                        length = int(stream.readline().split(b';',1)[0].strip(),16)
                        if not length:
                            while stream.readline().strip():
                                pass
                            return bytes(result)
                        result.extend(stream.read(length))
                        assert stream.read(2) == b'\r\n'
                body = chunks(self.rfile) if self.headers.get('Transfer-Encoding') == 'chunked' else self.rfile.read(int(self.headers.get('Content-Length', '0')))
                wire_bytes = len(body)
                if ('aws-chunked' in self.headers.get('Content-Encoding','') or
                        self.headers.get('X-Amz-Content-Sha256','').startswith('STREAMING-')):
                    body = chunks(io.BytesIO(body))
                status, data, headers = 200, b'', {}
                if self.command in ('PUT','POST'):
                    if 'create' not in query:
                        meter.objects[path] = body
                    headers['ETag'] = '"' + hashlib.md5(body).hexdigest() + '"'
                elif self.command == 'DELETE':
                    meter.objects.pop(path, None)
                    status = 204 if meter.protocol == 's3' else 200
                elif self.command in ('GET','HEAD'):
                    if meter.protocol == 's3' and (path in ('/bucket','bucket') or 'list-type' in query or 'prefix' in query):
                        prefix = query.get('prefix',[''])[0]
                        rows = [(name,value) for name,value in meter.objects.items() if name.startswith(prefix)]
                        data = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>bucket</Name>'
                                '<Prefix>'+escape(prefix)+'</Prefix><IsTruncated>false</IsTruncated>' + ''.join(
                                    '<Contents><Key>'+escape(name)+'</Key><Size>'+str(len(value))+'</Size>'
                                    '<LastModified>2026-10-28T00:00:00Z</LastModified><ETag>"'+hashlib.md5(value).hexdigest()+'"</ETag></Contents>'
                                    for name,value in rows)+'</ListBucketResult>').encode()
                    elif meter.protocol == 'node' and (path.endswith('/') or path == ''):
                        data = json.dumps([{'name':name[len(path):], 'size':len(value)} for name,value in meter.objects.items()
                                           if name.startswith(path) and '/' not in name[len(path):]]).encode()
                        headers['Content-Type'] = 'application/vnd.x.restic.rest.v2'
                    elif path in meter.objects:
                        data = meter.objects[path]
                        headers.update({'ETag':'"'+hashlib.md5(data).hexdigest()+'"',
                                        'Last-Modified':'Wed, 28 Oct 2026 00:00:00 GMT',
                                        'Content-Type':'application/octet-stream','Accept-Ranges':'bytes'})
                        if self.headers.get('Range'):
                            bounds = self.headers['Range'].removeprefix('bytes=').split('-')
                            start, end = int(bounds[0]), int(bounds[1]) if bounds[1] else len(data)-1
                            headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
                            data, status = data[start:end+1], 206
                    else:
                        status = 404
                        if meter.protocol == 's3':
                            data = b'<Error><Code>NoSuchKey</Code><Message>synthetic missing</Message></Error>'
                meter.events.append({'method':self.command,'upload':wire_bytes,'download':len(data) if self.command!='HEAD' else 0})
                self.send_response(status)
                for key,value in headers.items():
                    self.send_header(key,value)
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)

            do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = handle_request
        return Handler


def measure(binary, root, protocol):
    from acc import restic_process
    from acc.backup_s3 import S3Target
    from acc.backup_server import RestTarget
    original_run = restic_process.run
    def diagnostic(*args, **kwargs):
        result = original_run(*args, **kwargs)
        if result.returncode:
            print('Synthetic restic diagnostic:', result.stderr.decode(errors='replace')[:3000], flush=True)
        return result
    restic_process.run = diagnostic
    meter = Meter(protocol)
    server = ThreadingHTTPServer(('127.0.0.1',0),meter.handler())
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    key, token, credentials = root/'key', root/'token', root/'credentials'
    key.write_text('synthetic-password')
    token.write_text('synthetic-token')
    credentials.write_text(json.dumps({'access_key_id':'EXAMPLE','secret_access_key':'synthetic-secret'}))
    address = f'http://127.0.0.1:{server.server_port}'
    try:
        if protocol == 'node':
            backend = RestTarget(address+'/repo/',key,token,'example',binary)
        else:
            backend = S3Target({'endpoint':address,'bucket':'bucket','prefix':'repo','region':'us-east-1'},key,credentials,binary)
        backend.command_timeout = 90
        backend.command('init','--repository-version','2')
        payload = random.Random(42).randbytes(8*1024*1024)
        measurements = {}
        def command(label, *args, payload=None, output_file=None):
            meter.events.clear()
            with tempfile.TemporaryFile() as inp:
                if payload is not None:
                    inp.write(payload)
                    inp.seek(0)
                result = backend.command(*args, input_file=inp if payload is not None else None,
                                         output_file=output_file)
            measurements[label] = {'requests':len(meter.events), 'methods':dict(collections.Counter(x['method'] for x in meter.events)),
                                   'upload_bytes':sum(x['upload'] for x in meter.events),'download_bytes':sum(x['download'] for x in meter.events)}
            print(protocol,label,measurements[label],flush=True)
            return result
        # Same stdin archive path and JSON summary contract as Service.back_up.
        def capture(label, data):
            text = command(label,'backup','--json','--host','synthetic','--tag',label,'--stdin',
                           '--stdin-filename','acc-capture.tar',payload=data)
            summary = next(json.loads(line) for line in reversed(text.splitlines()) if json.loads(line).get('message_type')=='summary')
            measurements[label]['data_added_packed'] = summary.get('data_added_packed')
            return summary['snapshot_id']
        capture('initial_capture',payload)
        payload = payload[:4*1024*1024] + b'X'*4096 + payload[4*1024*1024+4096:]
        snapshot = capture('incremental_capture',payload)
        command('metadata_check','check')
        command('full_check','check','--read-data')
        with (root/'dump').open('wb') as out:
            command('snapshot_readback','dump',snapshot,'acc-capture.tar',output_file=out)
        assert (root/'dump').read_bytes() == payload
        measurements['stored_data_bytes'] = sum(len(value) for name,value in meter.objects.items() if '/data/' in name or name.startswith('data/'))
        return measurements
    finally:
        restic_process.run = original_run
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='backup-cost-meter-') as folder:
        root = Path(folder)
        output = {}
        for protocol in (sys.argv[3:4] or ('node','s3')):
            path = root/protocol
            path.mkdir()
            output[protocol] = measure(Path(sys.argv[1]),path,protocol)
            Path(sys.argv[2]).write_text(json.dumps(output,indent=2)+'\n')
        Path(sys.argv[2]).write_text(json.dumps(output,indent=2)+'\n')
