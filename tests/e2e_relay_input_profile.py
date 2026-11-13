"""Bounded fixture-only counters; never record SQL/parameters/prompt bytes."""
import contextvars
import os
import re
import time
from functools import wraps

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from acc.server.db import _TransactionGate
from acc.server.session_relay.terminal_grants import TerminalGrants


def install(ctx):
    rows, restores = [], []
    sampled = set()
    function_costs = contextvars.ContextVar('relay_frame_function_costs', default=None)
    current = contextvars.ContextVar('relay_input_profile', default=None)

    def before(_connection, _cursor, statement, _params, context, _many):
        row = current.get()
        if row is not None:
            context.relay_profile_started = time.perf_counter()
            kind = statement.lstrip().split(None, 1)[0].upper()
            key = 'selects' if kind == 'SELECT' else 'dml' if kind in ('UPDATE', 'INSERT', 'DELETE') else 'other_sql'
            row[key] += 1
            if key == 'selects':
                table = re.search(r'\bFROM\s+([a-z_]+)', statement, re.IGNORECASE)
                if table:
                    name = table[1].lower()
                    row['tables'][name] = row['tables'].get(name, 0) + 1

    def after(_connection, _cursor, _statement, _params, context, _many):
        row = current.get()
        if row is not None:
            row['sql_ms'] += (time.perf_counter() - context.relay_profile_started) * 1000

    event.listen(Engine, 'before_cursor_execute', before)
    event.listen(Engine, 'after_cursor_execute', after)

    def wrap(owner, name, build):
        original = getattr(owner, name)
        setattr(owner, name, build(original))
        restores.append((owner, name, original))

    def stage(name):
        def build(original):
            @wraps(original)
            def measured(*args, **kwargs):
                row = dict(stage=name, ms=0, sql_ms=0, gate_ms=0, commit_ms=0, selects=0, dml=0, other_sql=0, tables={})
                token = current.set(row)
                started = time.perf_counter()
                cpu_started = time.thread_time()
                profile_token = None
                if (os.environ.get('ACC_E2E_RELAY_CPU_PROFILE') == '1'
                        and name in ('frame', 'authorize', 'acknowledge') and name not in sampled
                        and (name == 'frame' or 'frame' in sampled)):
                    sampled.add(name)
                    profile_token = function_costs.set({})
                try:
                    return original(*args, **kwargs)
                finally:
                    if profile_token is not None:
                        costs = function_costs.get()
                        row['cpu_functions'] = [dict(function=key, calls=value[0], cumulative_ms=round(value[1], 4))
                            for key, value in sorted(costs.items(), key=lambda item: item[1][1], reverse=True)]
                        function_costs.reset(profile_token)
                    row['ms'] = (time.perf_counter() - started) * 1000
                    row['cpu_ms'] = (time.thread_time() - cpu_started) * 1000
                    current.reset(token)
                    if len(rows) < 512:
                        rows.append(row)
            return measured
        return build

    def cost(key):
        def build(original):
            @wraps(original)
            def measured(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    row = current.get()
                    if row is not None:
                        row[key] += (time.perf_counter() - started) * 1000
            return measured
        return build

    def function_cost(label):
        def build(original):
            @wraps(original)
            def measured(*args, **kwargs):
                active = function_costs.get()
                if active is None:
                    return original(*args, **kwargs)
                started = time.thread_time()
                try:
                    return original(*args, **kwargs)
                finally:
                    record = active.setdefault(label, [0, 0])
                    record[0] += 1
                    record[1] += (time.thread_time() - started) * 1000
            return measured
        return build

    from sqlalchemy.sql.elements import ClauseElement

    from acc.server import budgets, security_settings
    from acc.server.session_relay import authority
    from acc.server.session_relay.runtime import Launch, RequestResolver, RuntimeManager
    for owner, name, label in (
        (TerminalGrants, '_current', 'current'), (Launch, 'check_run', 'check_run'),
        (RuntimeManager, 'current_placement', 'placement'), (authority, 'current', 'authority'),
        (RequestResolver, 'authenticate_in_transaction', 'authenticate'),
        (security_settings, 'resolve', 'policy_resolve'), (security_settings, 'check_generation', 'generation'),
        (budgets, 'paused', 'budget'), (ctx, 'session_for', 'session_access'),
        (Session, '_execute_internal', 'orm_execute'), (ClauseElement, '_compile_w_cache', 'sql_compile_cache')):
        wrap(owner, name, function_cost(label))

    for name in ('frame', 'authorize', 'acknowledge', 'native_register', 'native_processed'):
        wrap(TerminalGrants, name, stage(name))
    wrap(_TransactionGate, 'enter', cost('gate_ms'))
    wrap(Session, 'commit', cost('commit_ms'))
    wrap(ctx, 'principal', stage('http_principal'))
    from acc.server.api import session_relay
    original_pool = session_relay.run_in_threadpool
    async def pool(work, *args, **kwargs):
        if getattr(work, '__name__', '') == 'rpc_work':
            work = stage('http_rpc_work')(work)
        return await original_pool(work, *args, **kwargs)
    session_relay.run_in_threadpool = pool
    restores.append((session_relay, 'run_in_threadpool', original_pool))
    from acc.server.session_relay import terminal_grants
    original_worker = terminal_grants.write_in_thread
    async def worker(work, *args):
        names = getattr(getattr(work, '__code__', None), 'co_names', ())
        label = next((name for name in ('frame', 'acknowledge', 'authorize', 'native_register', 'native_processed') if name in names), 'other')
        started = time.perf_counter()
        try:
            return await original_worker(work, *args)
        finally:
            if len(rows) < 512:
                rows.append({'stage': 'worker_total:' + label, 'ms': (time.perf_counter() - started) * 1000})
    terminal_grants.write_in_thread = worker
    restores.append((terminal_grants, 'write_in_thread', original_worker))
    original_input = TerminalGrants.input
    async def input_total(self, resolve, params, data):
        started = time.perf_counter()
        try:
            return await original_input(self, resolve, params, data)
        finally:
            if len(rows) < 512:
                rows.append({'stage': 'input_total', 'ms': (time.perf_counter() - started) * 1000})
    TerminalGrants.input = input_total
    restores.append((TerminalGrants, 'input', original_input))

    def close():
        for owner, name, original in reversed(restores):
            setattr(owner, name, original)
        event.remove(Engine, 'before_cursor_execute', before)
        event.remove(Engine, 'after_cursor_execute', after)
    return rows, close
