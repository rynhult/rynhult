"""Memory-only receipt keys, provisioned by kernel-authenticated supervisor PID.

The private datagram address is public, not an authority token. SO_PASSCRED binds
key provisioning to its real sender; the broker later selects only the pinned
supervisor PID/start time. The key is never in a manifest, environment or argv.
"""
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
from pathlib import Path

from acc import peercred
from acc.session_relay import protocol as p


def started(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]


def signature(key,name,record):
    return hmac.new(key,p.canonical_json([name,record]),hashlib.sha256).hexdigest()


class ReceiptKeys:
    def __init__(self,run_id,token,directory):
        self.run_id,self.token=run_id,token
        self.address=f'/proc/{os.getpid()}/fd/{directory}/receipt-key.sock'
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
            self.socket.bind(self.address);self.socket.settimeout(.1)
        except BaseException:
            self.socket.close();raise
        self.keys={};self.lock=threading.Lock();self.ended=threading.Event()
        self.thread=threading.Thread(target=self.receive,daemon=True);self.thread.start()

    def receive(self):
        while not self.ended.is_set():
            try:
                data,ancillary,flags,address=self.socket.recvmsg(8192,socket.CMSG_SPACE(peercred.UCRED.size))
                if flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC):continue
                credentials=[v for level,kind,v in ancillary if level==socket.SOL_SOCKET and kind==socket.SCM_CREDENTIALS]
                if len(credentials)!=1:continue
                pid,uid,_=peercred.UCRED.unpack(credentials[0])
                value=json.loads(data)
                if (uid!=os.getuid() or value['pid']!=pid or value['started']!=started(pid)
                        or value['runId']!=self.run_id or not hmac.compare_digest(value['token'],self.token)):
                    continue
                key=bytes.fromhex(value['key'])
                if len(key)!=32:continue
                with self.lock:
                    identity=(pid,value['started'])
                    if identity not in self.keys and len(self.keys)>=64:continue
                    previous=self.keys.setdefault(identity,key)
                    if not hmac.compare_digest(previous,key):continue
                self.socket.sendto(b'key-pinned',address)
            except (socket.timeout,ValueError,TypeError,KeyError,IndexError,OSError):
                continue  # missing/invalid provisioning is unknown, never positive cleanup

    def verify(self,name,record,pid,start):
        with self.lock:key=self.keys.get((pid,start))
        if key is None:return False
        body={k:v for k,v in record.items() if k!='hmac'}
        tag=record.get('hmac')
        return isinstance(tag,str) and hmac.compare_digest(tag,signature(key,name,body))

    def close(self):
        self.ended.set();self.socket.close();self.thread.join(.3)


def provision(manifest,directory):
    key=secrets.token_bytes(32)
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as channel:
        channel.bind(f'/proc/{os.getpid()}/fd/{directory}/receipt-client.sock');channel.settimeout(3)
        channel.sendto(p.canonical_json({'runId':manifest['runId'],'token':manifest['token'],
            'pid':os.getpid(),'started':started(os.getpid()),'key':key.hex()}),manifest['receiptSocket'])
        if channel.recv(32)!=b'key-pinned':raise ValueError('Supervisor key not pinned')
    os.unlink('receipt-client.sock',dir_fd=directory)
    return key
