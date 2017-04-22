# -*- coding: utf-8 -*-

import asyncio
import io
import mimetypes
import os
import tempfile
import json
import traceback
from concurrent.futures import ProcessPoolExecutor
from functools import wraps
import pathlib

import aiohttp
import pytest
from PIL import Image

from peony import exceptions
from peony import utils, general
from . import MockResponse, medias


def builtin_mimetypes(func):

    @wraps(func)
    async def decorated(session):
        magic = utils.magic
        mime = utils.mime

        utils.magic = None
        utils.mime = mimetypes.MimeTypes()

        try:
            await func(session)
        finally:
            utils.magic = magic
            utils.mime = mime

    return decorated


@pytest.fixture
def json_data():
    return utils.JSONData({'a': 1, 'b': 2})


@pytest.fixture
def response(json_data):
    return utils.PeonyResponse(data=json_data,
                               headers={},
                               url="",
                               request={})


@pytest.fixture
def executor():
    return ProcessPoolExecutor()


@pytest.yield_fixture
def session(event_loop):
    session = aiohttp.ClientSession(loop=event_loop)
    yield session
    event_loop.run_until_complete(session.close())


def test_json_data_get(json_data):
    assert json_data.a == json_data['a'] == 1
    assert json_data.b == json_data['b'] == 2


def test_json_data_set(json_data):
    json_data.c = 1
    json_data['d'] = 2
    assert json_data.c == json_data['c'] == 1
    assert json_data.d == json_data['d'] == 2


def test_json_data_del(json_data):
    del json_data.a
    del json_data['b']
    assert not hasattr(json_data, 'a') and 'a' not in json_data
    assert not hasattr(json_data, 'b') and 'b' not in json_data


def test_response_get(response):
    assert response.a == response['a'] == response.data.a


def test_response_set(response):
    response.a = 3
    response['b'] = 4
    assert response.a == response['a'] == 3
    assert response.b == response['b'] == 4


def test_response_del(response):
    del response.a
    del response['b']
    assert not hasattr(response, 'a') and 'a' not in response
    assert not hasattr(response, 'b') and 'b' not in response


def test_response_iter():
    resp = utils.PeonyResponse(list(range(3)), {}, "", {})
    for i, x in enumerate(resp):
        assert i == x


def test_response_str(response):
    assert str(response) == str(response.data)


def test_response_repr(response):
    assert repr(response) == repr(response.data)


def test_response_len(response):
    assert len(response) == len(response.data)


@pytest.mark.asyncio
async def test_error_handler_rate_limit():
    global tries
    tries = 3

    async def rate_limit(**kwargs):
        global tries
        tries -= 1

        if tries > 0:
            response = MockResponse(error=88,
                                    headers={'X-Rate-Limit-Reset': 0})
            raise await exceptions.throw(response)

    await utils.error_handler(rate_limit)()


@pytest.mark.asyncio
async def test_error_handler_asyncio_timeout():
    global tries
    tries = 3

    async def timeout(**kwargs):
        global tries
        tries -=1

        if tries > 0:
            raise asyncio.TimeoutError

    await utils.error_handler(timeout)()


@pytest.mark.asyncio
async def test_error_handler_other_exception():
    async def error(**kwargs):
        raise exceptions.PeonyException

    with pytest.raises(exceptions.PeonyException):
        await utils.error_handler(error)()


@pytest.mark.asyncio
async def test_error_handler_response():
    async def request(**kwargs):
        return MockResponse(data=MockResponse.message)

    resp = await utils.error_handler(request)()
    text = await resp.text()
    assert text == MockResponse.message


def test_get_args():
    def test(a, b, c):
        pass

    assert utils.get_args(test) == ('a', 'b', 'c')
    assert utils.get_args(test, skip=1) == ('b', 'c')
    assert utils.get_args(test, skip=3) == tuple()


def test_format_error():
    try:
        raise RuntimeError
    except RuntimeError:
        output = utils.format_error(MockResponse.message)
        assert traceback.format_exc().strip() in output
        assert MockResponse.message in output
        assert traceback.format_exc().strip() in utils.format_error()


def test_print_error():
    out = io.StringIO()
    try:
        raise RuntimeError
    except RuntimeError:
        utils.print_error(MockResponse, stderr=out, end='')
        out.seek(0)
        assert utils.format_error(MockResponse) == out.read()


def test_loads():
    j = utils.loads("""{"a": 1, "b": 2}""")
    assert isinstance(j, utils.JSONData)
    assert j.a == 1 and j.b == 2


@pytest.mark.asyncio
async def test_reset_io():
    @utils.reset_io
    async def test(media):
        assert media.tell() == 0
        media.write(MockResponse.message)
        assert media.tell() != 0

    f = io.StringIO()
    f.write("Hello World")
    assert f.tell() != 0
    await test(f)
    assert f.tell() == 0


@pytest.mark.asyncio
async def test_get_type(session):
    async def test(media, chunk_size=1024):
        f = io.BytesIO(await media.download(session, chunk_size))
        media_type, media_category = await utils.get_type(f)
        assert media_type == media.type
        assert media_category == media.category

    tasks = [test(media) for media in medias.values()]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_get_type_exception():
    with pytest.raises(RuntimeError):
        await utils.get_type(io.BytesIO())


@pytest.mark.asyncio
@builtin_mimetypes
async def test_get_type_builtin(session):
    async def test(media, chunk_size=1024):
        f = io.BytesIO(await media.download(session, chunk_size))
        media_type, media_category = await utils.get_type(f, media.filename)
        assert media_type == media.type
        assert media_category == media.category

    tasks = [test(media) for media in medias.values()]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
@builtin_mimetypes
async def test_get_type_builtin_exception(session):
    media = medias['lady_peony']
    f = io.BytesIO(await media.download(session, 1024))
    with pytest.raises(RuntimeError):
        await utils.get_type(f)


@pytest.mark.asyncio
async def test_get_size():
    f = io.BytesIO(bytes(10000))
    assert await utils.get_size(f) == 10000
    assert f.tell() == 0


@pytest.mark.asyncio
async def test_execute():
    def test():
        return 1

    async def async_test():
        return 1

    assert await utils.execute(test()) == 1
    assert await utils.execute(async_test()) == 1


def convert(img, formats):
    imgs = []
    for kwargs in formats:
        i = io.BytesIO()
        img.save(i, **kwargs)
        imgs.append(i)

    return imgs


def get_size(f):
    f.seek(0, os.SEEK_END)
    return f.tell()


@pytest.mark.asyncio
async def test_convert(event_loop, session, executor):

    async def test(media):
        data = await media.download(session)

        f = io.BytesIO(data)

        img = Image.open(f)

        conv = await event_loop.run_in_executor(executor, utils.convert,
                                                img, general.formats)
        results = await event_loop.run_in_executor(executor, convert,
                                                   img, general.formats)
        smallest = min([get_size(f) for f in results])
        assert smallest == get_size(conv)

    tasks = [test(media) for key, media in medias.items()
             if key in ('lady_peony', 'seismic_waves')]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_optimize_media(event_loop, session, executor):
    async def test(media):
        data = await media.download(session)

        f = io.BytesIO(data)
        media = await event_loop.run_in_executor(
            executor, utils.optimize_media, f, (1024, 512), general.formats
        )
        img = Image.open(media)
        assert img.size[0] <= 1024 and img.size[1] <= 512

        media = await event_loop.run_in_executor(
            executor, utils.optimize_media, f, (512, 1024), general.formats
        )
        img = Image.open(media)
        assert img.size[0] <= 512 and img.size[1] <= 1024

    tasks = [test(media) for key, media in medias.items()
             if key in ('lady_peony', 'seismic_waves', 'pink_queen')]
    await asyncio.gather(*tasks)


def test_optimize_media_exception():
    PIL = utils.PIL
    utils.PIL = None

    try:
        with pytest.raises(RuntimeError):
            utils.optimize_media(io.BytesIO(), (100, 100), [])
    finally:
        utils.PIL = PIL


@pytest.mark.asyncio
async def test_optimize_media_with_filename(session):
    with tempfile.NamedTemporaryFile('w+b') as tmp:
        data = await medias['lady_peony'].download(session)
        tmp.write(data)

        media1 = utils.optimize_media(tmp.name, (100, 100), general.formats)
        media2 = utils.optimize_media(tmp, (100, 100), general.formats)

        assert not tmp.closed
        assert get_size(media1) == get_size(media2)


@pytest.mark.asyncio
async def test_get_media_metadata(session):
    async def test(media):
        data = await media.download(session, 1024)
        f = io.BytesIO(data)
        media_metadata = await utils.get_media_metadata(f)
        assert media_metadata == (media.type, media.category,
                                  media.type.startswith('image'), f)

    tasks = [test(media) for media in medias.values()]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_get_media_metadata_filename(session):
    media = medias['lady_peony']
    with tempfile.NamedTemporaryFile('w+b') as tmp:
        data = await media.download(session)
        tmp.write(data)

        file1_metadata = await utils.get_media_metadata(tmp.name)
        file2_metadata = await utils.get_media_metadata(tmp)

        assert all(file1_metadata[i] == file2_metadata[i] for i in range(3))
        assert file1_metadata[3] == tmp.name


@pytest.mark.asyncio
async def test_get_media_metadata_path(session):
    media = medias['lady_peony']
    with tempfile.NamedTemporaryFile('w+b') as tmp:
        data = await media.download(session)
        tmp.write(data)

        path = pathlib.Path(tmp.name)
        file1_metadata = await utils.get_media_metadata(path)
        file2_metadata = await utils.get_media_metadata(tmp)

        assert all(file1_metadata[i] == file2_metadata[i] for i in range(3))
        assert file1_metadata[3] == tmp.name


@pytest.mark.asyncio
async def test_get_media_metadata_bytes(session):
    media = medias['lady_peony']
    data = await media.download(session)
    f = io.BytesIO(data)

    file1_metadata = await utils.get_media_metadata(data)
    file2_metadata = await utils.get_media_metadata(f)

    assert all(file1_metadata[i] == file2_metadata[i] for i in range(3))
    assert isinstance(file1_metadata[3], io.BytesIO)


@pytest.mark.asyncio
async def test_get_media_metadata_exception():
    with pytest.raises(TypeError):
        await utils.get_media_metadata([])


@pytest.mark.asyncio
async def test_read(json_data):
    response = MockResponse(data=MockResponse.message,
                            content_type="text/plain")
    assert await utils.read(response) == MockResponse.message

    response = MockResponse(data=json.dumps(json_data),
                            content_type="application/json")

    data = await utils.read(response)
    assert all(data[key] == json_data[key]
               for key in {*data.keys(), *json_data.keys()})

    response = MockResponse(data=MockResponse.message,
                            content_type="application/octet-stream")
    assert await utils.read(response) == MockResponse.message.encode()


@pytest.mark.asyncio
async def test_read_decode_error():
    response = MockResponse(data=b'\x80', content_type="text/plain")
    try:
        await utils.read(response, encoding='utf-8')
    except exceptions.PeonyDecodeError as exc:
        assert exc.data == b'\x80'
        assert isinstance(exc.exception, UnicodeDecodeError)
    else:
        pytest.fail("Did not raise PeonyDecoderError")


@pytest.mark.asyncio
async def test_read_json_decode_error():
    response = MockResponse(data='{', content_type="application/json")
    try:
        await utils.read(response, encoding='utf-8')
    except exceptions.PeonyDecodeError as exc:
        assert exc.data == b'{'
        assert isinstance(exc.exception, json.JSONDecodeError)
    else:
        pytest.fail("Did not raise PeonyDecoderError")