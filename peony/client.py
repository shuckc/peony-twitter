# -*- coding: utf-8 -*-
"""
Peony Clients

:class:`BasePeonyClient` only handles requests while
:class:`PeonyClient` adds some methods that could help when using
the Twitter APIs, with a method to upload a media
"""

import asyncio
import io
import logging
from concurrent.futures import ProcessPoolExecutor
from urllib.parse import urlparse

import aiohttp

from . import data_processing, exceptions, general, oauth, utils
from .api import APIPath, StreamingAPIPath
from .commands import EventStreams, init_task, task
from .oauth import OAuth1Headers
from .stream import StreamResponse

logger = logging.getLogger(__name__)


class MetaPeonyClient(type):

    def __new__(cls, name, bases, attrs, **kwargs):
        """ put the :class:`~peony.commands.tasks.Task`s in the right place """
        tasks = {'init_tasks': set(), 'tasks': set()}

        for base in bases:
            if hasattr(base, '_tasks'):
                for key, value in base._tasks.items():
                    tasks[key] |= value

        for attr in attrs.values():
            if isinstance(attr, init_task):
                tasks['init_tasks'].add(attr)
            elif isinstance(attr, task):
                tasks['tasks'].add(attr)

        attrs['_tasks'] = tasks

        return super().__new__(cls, name, bases, attrs)


class BasePeonyClient(metaclass=MetaPeonyClient):
    """
        Access the Twitter API easily

    You can create tasks by decorating a function from a child
    class with :class:`peony.task`

    You also attach a :class:`EventStream` to a subclass using
    the :func:`event_stream` of the subclass

    After creating an instance of the child class you will be able
    to run all the tasks easily by executing :func:`get_tasks`

    Parameters
    ----------
    streaming_apis : iterable, optional
        Iterable containing the streaming APIs subdomains
    base_url : str, optional
        Format of the url for all the requests
    api_version : str, optional
        Default API version
    suffix : str, optional
        Default suffix of API endpoints
    loads : function, optional
        Function used to load JSON data
    error_handler : function, optional
        Requests decorator
    session : aiohttp.ClientSession, optional
        Session to use to make requests
    proxy : str
        Proxy used with every request
    compression : bool, optional
        Activate data compression on every requests, defaults to True
    user_agent : str, optional
        Set a custom user agent header
    encoding : str, optional
        text encoding of the response from the server
    loop : event loop, optional
        An event loop, if not specified :func:`asyncio.get_event_loop`
        is called
    """

    _streams = EventStreams()

    def __init__(self,
                 consumer_key=None,
                 consumer_secret=None,
                 access_token=None,
                 access_token_secret=None,
                 bearer_token=None,
                 auth=None,
                 headers=None,
                 streaming_apis=None,
                 base_url=None,
                 api_version=None,
                 suffix='.json',
                 loads=data_processing.loads,
                 error_handler=utils.error_handler,
                 session=None,
                 proxy=None,
                 compression=True,
                 user_agent=None,
                 encoding=None,
                 loop=None,
                 **kwargs):

        if streaming_apis is None:
            self.streaming_apis = general.streaming_apis
        else:
            self.streaming_apis = streaming_apis

        if base_url is None:
            self.base_url = general.twitter_base_api_url
        else:
            self.base_url = base_url

        if api_version is None:
            self.api_version = general.twitter_api_version
        else:
            self.api_version = api_version

        if auth is None:
            auth = OAuth1Headers

        self.proxy = proxy

        self._suffix = suffix

        self.error_handler = error_handler

        self.encoding = encoding

        if encoding is not None:
            def _loads(*args, **kwargs):
                return loads(*args, encoding=encoding, **kwargs)

            self._loads = _loads
        else:
            self._loads = loads

        self.loop = asyncio.get_event_loop() if loop is None else loop

        self._session = session
        self._user_session = session is not None

        self._gathered_tasks = None

        if consumer_key is None or consumer_secret is None:
            raise TypeError("missing 2 required arguments: 'consumer_key' "
                            "and 'consumer_secret'")

        # all the possible args required by headers in :mod:`peony.oauth`
        kwargs = {
            'consumer_key': consumer_key,
            'consumer_secret': consumer_secret,
            'access_token': access_token,
            'access_token_secret': access_token_secret,
            'bearer_token': bearer_token,
            'compression': compression,
            'user_agent': user_agent,
            'headers': headers,
            'client': self
        }

        # get the args needed by the auth parameter on initialization
        args = utils.get_args(auth.__init__, skip=1)

        # keep only the arguments required by auth on init
        kwargs = {key: value for key, value in kwargs.items()
                  if key in args}

        self.headers = auth(**kwargs)

        self.__setup = {'done': asyncio.Event(),
                        'early': asyncio.Event(),
                        'state': False}

    def init_tasks(self):
        """ tasks executed on initialization """
        return self._get_tasks(kind=init_task)

    async def setup(self, early=False):
        """
            set up the client on the first request
        """
        if not self.__setup['state']:
            self.__setup['state'] = True

            if self._session is None:
                self._session = aiohttp.ClientSession()

            self.__setup['early'].set()

            init_tasks = self.init_tasks
            if callable(init_tasks):
                init_tasks = init_tasks()

            if init_tasks:
                await asyncio.wait(init_tasks)

            self.__setup['done'].set()

        if early:
            await self.__setup['early'].wait()
        else:
            await self.__setup['done'].wait()

    @staticmethod
    def _get_base_url(base_url, api, version):
        """
            create the base url for the api

        Parameters
        ----------
        base_url : str
            format of the base_url using {api} and {version}
        api : str
            name of the api to use
        version : str
            version of the api

        Returns
        -------
        str
            the base url of the api you want to use
        """
        format_args = {}

        if "{api}" in base_url:
            if api == "":
                base_url = base_url.replace('{api}.', '')
            else:
                format_args['api'] = api

        if "{version}" in base_url:
            if version == "":
                base_url = base_url.replace('/{version}', '')
            else:
                format_args['version'] = version

        return base_url.format(api=api, version=version)

    def __getitem__(self, values):
        """
            Access the api you want

        This permits the use of any API you could know about

        For most api you only need to type

        >>> self[api]  # api is the api you want to access

        You can specify a custom api version using the syntax

        >>> self[api, version]  # version is the api version as a str

        For more complex requests

        >>> self[api, version, suffix, base_url]

        Returns
        -------
        .api.BaseAPIPath
            To access an API endpoint
        """
        defaults = None, self.api_version, self._suffix, self.base_url
        keys = ['api', 'version', 'suffix', 'base_url']

        if isinstance(values, dict):
            # set values in the right order
            values = [values.get(key, defaults[i])
                      for i, key in enumerate(keys)]
        elif isinstance(values, set):
            raise TypeError('Cannot use a set to access an api, '
                            'please use a dict, a tuple or a list instead')
        elif isinstance(values, str):
            values = [values, *defaults[1:]]
        elif isinstance(values, tuple):
            if len(values) < len(keys):
                padding = (None,) * (len(keys) - len(values))
                values += padding

            values = [default if value is None else value
                      for value, default in zip(values, defaults)
                      if (value, default) != (None, None)]
        else:
            raise TypeError("Could not create an endpoint from an object of "
                            "type " + values.__class__.__name__)

        api, version, suffix, base_url = values

        base_url = self._get_base_url(base_url, api, version)

        # use StreamingAPIPath if subdomain is in self.streaming_apis
        if api in self.streaming_apis:
            return StreamingAPIPath([base_url], suffix=suffix, client=self)
        else:
            return APIPath([base_url], suffix=suffix, client=self)

    __getattr__ = __getitem__

    def __del__(self):
        self.close()

    async def request(self, method, url,
                      headers=None,
                      session=None,
                      encoding=None,
                      **kwargs):
        """
            Make requests to the REST API

        Parameters
        ----------
        method : str
            Method to be used by the request
        url : str
            URL of the resource
        headers : .oauth.PeonyHeaders
            Custom headers (doesn't overwrite `Authorization` headers)
        session : aiohttp.ClientSession, optional
            Client session used to make the request

        Returns
        -------
        data.PeonyResponse
            Response to the request
        """
        await self.setup(early=True)

        # prepare request arguments, particularly the headers
        req_kwargs = await self.headers.prepare_request(
            method=method,
            url=url,
            headers=headers,
            **kwargs
        )

        if encoding is None:
            encoding = self.encoding

        if 'proxy' not in req_kwargs:
            req_kwargs['proxy'] = self.proxy

        session = session if (session is not None) else self._session

        logger.debug("making request with parameters: %s" % req_kwargs)

        async with session.request(**req_kwargs) as response:
            if response.status < 400:
                data = await data_processing.read(response, self._loads,
                                                  encoding=encoding)

                return data_processing.PeonyResponse(
                    data=data,
                    headers=response.headers,
                    url=response.url,
                    request=req_kwargs
                )
            else:  # throw exception if status is not 2xx
                await exceptions.throw(response, loads=self._loads,
                                       encoding=encoding)

    def stream_request(self, method, url, headers=None, _session=None,
                       *args, **kwargs):
        """
            Make requests to the Streaming API

        Parameters
        ----------
        method : str
            Method to be used by the request
        url : str
            URL of the resource
        headers : dict
            Custom headers (doesn't overwrite `Authorization` headers)
        _session : aiohttp.ClientSession, optional
            The session to use for this specific request, the session
            given as argument of :meth:`__init__` is used by default

        Returns
        -------
        .stream.StreamResponse
            Stream context for the request
        """
        return StreamResponse(
            method=method,
            url=url,
            client=self,
            headers=headers,
            session=_session,
            **kwargs
        )

    @classmethod
    def event_stream(cls, event_stream):
        """ Decorator to attach an event stream to the class """
        cls._streams.append(event_stream)
        return event_stream

    def _get_tasks(self, kind=task):
        if kind == task:
            key = 'tasks'
        elif kind == init_task:
            key = 'init_tasks'
        else:
            raise RuntimeError("Cannot get tasks of kind %s" % kind)

        return [task(self) for task in self._tasks[key]]

    def get_tasks(self):
        """
            Get the tasks attached to the instance

        Returns
        -------
        list
            List of tasks (:class:`asyncio.Task`)
        """
        tasks = self._get_tasks()

        tasks.extend(self._streams.get_tasks(self))

        return tasks

    async def run_tasks(self):
        """ Run the tasks attached to the instance """
        await self.setup()
        tasks = self.get_tasks()
        self._gathered_tasks = asyncio.gather(*tasks, loop=self.loop)
        await asyncio.wait(tasks)

    def run(self):
        """ Run the tasks attached to the instance """
        try:
            self.loop.run_until_complete(self.run_tasks())
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self):
        """ properly close the client """
        # close the session only if it was created by peony
        if not self._user_session:
            # close is None for Python 3.5 here (?)
            try:
                self._session.close()
                self._session = None
            except (TypeError, AttributeError):
                pass

        # close currently running tasks
        if self._gathered_tasks is not None:
            try:
                self._gathered_tasks.cancel()
                if not self.loop.is_closed():
                    self.loop.run_until_complete(self._gathered_tasks)

                self._gathered_tasks.exception()
            except:
                pass


class PeonyClient(BasePeonyClient):
    """
        A client with some useful methods for most usages
    """

    def __init__(self, *args, executor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.executor = ProcessPoolExecutor() if executor is None else executor

    def init_tasks(self):
        tasks = super().init_tasks()
        if isinstance(self.headers, oauth.OAuth1Headers):
            tasks.append(self.__get_user())
        return tasks

    @init_task
    async def __get_twitter_configuration(self):
        """
        create a ``twitter_configuration`` attribute with the response
        of the endpoint
        https://api.twitter.com/1.1/help/configuration.json
        """
        api = self['api', general.twitter_api_version,
                   ".json", general.twitter_base_api_url]

        req = api.help.configuration.get()
        self.twitter_configuration = await req

    async def __get_user(self):
        """
        create a ``user`` attribute with the response of the endpoint
        https://api.twitter.com/1.1/account/verify_credentials.json
        """
        api = self['api', general.twitter_api_version,
                   ".json", general.twitter_base_api_url]

        req = api.account.verify_credentials.get()
        self.user = await req

    async def _chunked_upload(self, media,
                              path=None,
                              media_type=None,
                              media_category=None,
                              chunk_size=2**20,
                              **params):
        """
            upload media in chunks

        Parameters
        ----------
        media : file object
            a file object of the media
        path : str, optional
            filename of the media
        media_type : str, optional
            mime type of the media
        media_category : str, optional
            twitter media category, must be used with ``media_type``
        chunk_size : int, optional
            size of a chunk in bytes
        params : dict, optional
            additional parameters of the request

        Returns
        -------
        .data_processing.PeonyResponse
            Response of the request
        """
        media_size = await utils.get_size(media)

        if media_type is None:
            media_metadata = await utils.get_media_metadata(media, path)
            media_type, media_category = media_metadata
        elif media_category is None:
            media_category = utils.get_category(media_type)

        response = await self.upload.media.upload.post(
            command="INIT",
            total_bytes=media_size,
            media_type=media_type,
            media_category=media_category,
            **params
        )

        media_id = response['media_id']

        async for i, chunk in utils.chunks(media, chunk_size):
            await self.upload.media.upload.post(command="APPEND",
                                                media_id=media_id,
                                                media=chunk,
                                                segment_index=i)

        status = await self.upload.media.upload.post(command="FINALIZE",
                                                     media_id=media_id)

        if 'processing_info' in status:
            while status['processing_info'].get('state') != "succeeded":
                processing_info = status['processing_info']
                if processing_info.get('state') == "failed":
                    error = processing_info.get('error', {})

                    message = error.get('message', str(status))

                    raise exceptions.MediaProcessingError(data=status,
                                                          message=message,
                                                          **params)

                delay = processing_info['check_after_secs']
                await asyncio.sleep(delay)

                status = await self.upload.media.upload.get(
                    command="STATUS",
                    media_id=media_id,
                    **params
                )

        return response

    async def _size_test(self, media, size_limit):
        if size_limit is None:
            if isinstance(self.twitter_configuration, APIPath):
                return False
            else:
                try:
                    size_limit = self.twitter_configuration['photo_size_limit']
                except KeyError:
                    return False

        return await utils.get_size(media) > size_limit

    async def upload_media(self, file_,
                           media_type=None,
                           media_category=None,
                           chunked=None,
                           size_limit=None,
                           **params):
        """
            upload a media on twitter

        Parameters
        ----------
        file_ : str or pathlib.Path or file
            Path to the file or file object
        media_type : str, optional
            mime type of the media
        media_category : str, optional
            Twitter's media category of the media, must be used with
            ``media_type``
        chunked : bool, optional
            If True, force the use of the chunked upload for the media
        size_limit : int, optional
            If set, the media will be sent using a multipart upload if
            its size is over ``size_limit`` bytes
        params : dict
            parameters used when making the request

        Returns
        -------
        .data_processing.PeonyResponse
            Response of the request
        """
        if isinstance(file_, str):
            path = urlparse(file_).path.strip(" \"'")
            media = await utils.execute(open(path, 'rb'))
        elif hasattr(file_, 'read'):
            media = file_
        elif isinstance(file_, bytes):
            media = io.BytesIO(file_)
        else:
            raise TypeError("upload_media input must be a file object or a "
                            "filename or binary data")

        size_test = await self._size_test(media, size_limit)

        if (size_test and chunked is None) or chunked:
            args = media, file_, media_type, media_category
            response = await self._chunked_upload(*args, **params)
        else:
            response = await self.upload.media.upload.post(media=media,
                                                           **params)

        if not hasattr(file_, 'read') and not media.closed:
            media.close()

        return response
