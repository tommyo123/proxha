import asyncio


async def run(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            # Keep the caller's lock until the database worker has finished.
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
