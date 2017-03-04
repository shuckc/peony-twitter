#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio

try:
    from . import peony, api, testdir
except (SystemError, ImportError):
    from __init__ import peony, testdir
    import api

loop = asyncio.get_event_loop()
client = peony.PeonyClient(**api.keys, loop=loop)


async def search_test():
    print(await client.api.search.tweets.get(q="Watson AND IBM"))

if __name__ == '__main__':
    loop.run_until_complete(search_test())