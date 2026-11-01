"""Never connect a disposable browser instance to the host's privileged folder worker."""
from acc import folder_channel


async def unavailable(message):
    raise OSError('No real root folder worker is available to this disposable instance.')


def install():
    folder_channel.request = unavailable
