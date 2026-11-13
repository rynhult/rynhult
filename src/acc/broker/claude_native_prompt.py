"""Read-only native modal detection. This module has no input/answer transport."""
import asyncio
import re

from acc.session_relay import protocol as p


def overwrite_modal(screen):
    if not isinstance(screen, str) or len(screen.encode()) > 128 << 10:
        return False
    lines = [line.strip() for line in screen.splitlines()]
    footers = [i for i, line in enumerate(lines) if line == 'Esc to cancel · Tab to amend']
    if not footers:
        return False
    footer = footers[-1]
    frame = lines[:footer]
    questions = [i for i, line in enumerate(frame) if re.fullmatch(r'Do you want to overwrite .+\?', line)]
    if not questions or any(line for line in lines[footer + 1:]):
        return False
    question = questions[-1]
    return ('Overwrite file' in frame[:question] and
            any(re.fullmatch(r'[❯>]\s*1\. Yes', line) for line in frame[question + 1:]) and
            any(re.fullmatch(r'3\. No', line) for line in frame[question + 1:]))


async def watch(factory, owner, observer, hooks, spec):
    stop = getattr(factory, 'native_prompt_stop', None)
    if stop is None:
        stop = factory.native_prompt_stop = asyncio.Event()
    previous = False
    try:
        while not stop.is_set() and not factory.closed and factory.current is owner:
            try:
                screen = await observer.capture(owner.host.run, history=False)
                active = not hooks.pending and overwrite_modal(screen['text'])
                # Capture awaited authority/process checks. A new hook question
                # or replacement owner must not become a screen-derived card.
                if stop.is_set() or factory.current is not owner or factory.hooks is not hooks or factory.runtime.spec != spec:
                    return
                factory.runtime.native_prompt_active = active
                owner.host.native_prompt_active = active
                if active != previous:
                    await factory.runtime.publish({'kind': 'native.prompt', 'active': active})
                    previous = active
            except (OSError, ValueError, ConnectionError, TimeoutError, p.RelayError):
                pass  # loss is not evidence the native prompt was answered
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        raise


async def close(factory):
    task = getattr(factory, 'native_prompt_task', None)
    factory.native_prompt_task = None
    if task:
        stop = getattr(factory, 'native_prompt_stop', None)
        if stop:
            stop.set()
        _, pending = await asyncio.wait({task}, timeout=5)
        if pending:
            task.cancel()  # genuinely stuck authority must fence its shared peer
        await asyncio.gather(task, return_exceptions=True)
