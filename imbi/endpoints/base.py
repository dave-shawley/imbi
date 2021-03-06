"""
Base Request Handlers

"""
import datetime
import logging
import typing
import uuid
from email import utils

import jsonpatch
import problemdetails
import sprockets_postgres as postgres
from sprockets.http import mixins
from sprockets.mixins import correlation, mediatype
from tornado import httputil, web

from imbi import __version__, common, session, user

LOGGER = logging.getLogger(__name__)


def require_permission(permission):
    """Decorator function for requiring a permission string for an endpoint

    :param str permission: The permission string to require
    :raises: web.HTTPError(403)

    """
    def _require_permission(f):
        def wrapped(self, *args, **kwargs):
            """Inner-wrapping of the decorator that performs the logic"""
            if not self._current_user or \
                    not self._current_user.has_permission(permission):
                if self._respond_with_html:
                    return self.render('index.html')
                LOGGER.info('%r does not have the "%s" permission',
                            self._current_user, permission)
                raise web.HTTPError(403, 'Unauthorized')
            return f(self, *args, **kwargs)
        return wrapped
    return _require_permission


class RequestHandler(mixins.ErrorLogger,
                     problemdetails.ErrorWriter,
                     postgres.RequestHandlerMixin,
                     correlation.HandlerMixin,
                     mediatype.ContentMixin,
                     web.RequestHandler):
    """Base RequestHandler class used for recipients and subscribers."""

    APPLICATION_JSON = 'application/json'
    TEXT_HTML = 'text/html'
    NAME = 'Base'

    def __init__(self,
                 application,
                 request: httputil.HTTPServerRequest,
                 **kwargs):
        super().__init__(application, request, **kwargs)
        self.session: typing.Optional[session.Session] = None
        self._current_user: typing.Optional[user.User] = None
        self._links = {}

    async def prepare(self) -> None:
        """Prepare the request handler for the request. If the application
        is not ready return a ``503`` error.

        Checks for a session cookie and if present, loads the session into
        the current user and authenticates it. If authentication fails,
        the current user and cookie is cleared.

        """
        if not self.application.ready_to_serve:
            return self.send_error(503, reason='Application not ready')
        self.session = session.Session(self)
        await self.session.initialize()
        self._current_user = await self.get_current_user()
        await super().prepare()

    def on_finish(self) -> None:
        """Invoked after a request has completed"""
        super().on_finish()
        metric_id = '{}.{}'.format(self.NAME, self.request.method)
        self.application.loop.add_callback(
            self.application.stats.incr,
            'response.{}.{}'.format(metric_id, self.get_status()))
        self.application.loop.add_callback(
            self.application.stats.add_duration,
            'request.{}.{}'.format(metric_id, self.get_status()),
            self.request.request_time())

    def compute_etag(self) -> None:
        """Override Tornado's built-in ETag generation"""
        return None

    async def get_current_user(self) -> typing.Optional[user.User]:
        """Used by the system to manage authentication behaviors."""
        if self.session and self.session.user:
            return self.session.user
        token = self.request.headers.get('Private-Token', None)
        if token:
            current_user = user.User(self.application, token=token)
            if await current_user.authenticate():
                return current_user

    def get_template_namespace(self) -> dict:
        """Returns a dictionary to be used as the default template namespace.

        The results of this method will be combined with additional defaults
        in the :mod:`tornado.template` module and keyword arguments to
        :meth:`~tornado.web.RequestHandler.render`
        or :meth:`~tornado.web.RequestHandler.render_string`.

        """
        namespace = super(RequestHandler, self).get_template_namespace()
        namespace.update({'__version__': __version__})
        return namespace

    def send_response(self, value: typing.Union[dict, list]) -> None:
        """Send the response to the client"""
        if 'self' not in self._links:
            self._add_self_link(self.request.path)
            self._add_link_header()
        if hasattr(self, 'TTL') and \
                not self.request.headers.get('Pragma') == 'no-cache':
            self._add_response_caching_headers(self.TTL)
        super().send_response(value)

    def set_default_headers(self) -> None:
        """Override the default headers, setting the Server response header"""
        super().set_default_headers()
        self.set_header('Server', '{}/{}'.format(
            self.settings['service'], __version__))

    def _add_last_modified_header(self, value: datetime.datetime) -> None:
        """Add a RFC-822 formatted timestamp for the Last-Modified HTTP
        response header.

        """
        if not value:
            return self.logger.debug('Last-Modified received None value')
        self.set_header('Last-Modified', self._rfc822_date(value))

    def _add_link_header(self) -> None:
        """Takes the accumulated links and creates a link header value"""
        links = []
        for rel, path in self._links.items():
            links.append('<{}://{}{}>; rel="{}"'.format(
                self.request.protocol, self.request.host, path, rel))
        if links:
            self.add_header('Link', ','.join(links))

    def _add_self_link(self, path: str) -> None:
        """Adds the self Link response header"""
        self._links['self'] = path

    def _add_response_caching_headers(self, ttl: int) -> None:
        """Adds the cache response headers for the object being returned."""
        self.add_header('Cache-Control', 'public, max-age={}'.format(ttl))

    def _on_postgres_timing(self,
                            metric_name: str,
                            duration: float) -> None:
        """Invoked by sprockets-postgres after each query"""
        self.application.loop.add_callback(
            self.application.stats.add_duration,
            'postgres.{}'.format(metric_name), duration)

    @property
    def _respond_with_html(self) -> bool:
        """Returns True if the current response should respond with HTML"""
        return self.get_response_content_type().startswith(self.TEXT_HTML)

    @staticmethod
    def _rfc822_date(value: datetime.datetime) -> str:
        """Return an RFC-822 formatted timestamp for the given value"""
        return utils.format_datetime(value)


class AuthenticatedRequestHandler(RequestHandler):
    """RequestHandler base class for authenticated requests"""

    async def prepare(self) -> None:
        await super().prepare()
        if not self._current_user:
            if self._respond_with_html:
                return await self.render('index.html')
            self.set_status(401)
            self.finish()


class CRUDRequestHandler(AuthenticatedRequestHandler):
    """CRUD request handler to reduce large amounts of duplicated code"""

    NAME = 'default'
    DEFAULTS = {}
    ID_KEY = 'id'
    ITEM_SCHEMA = None
    FIELDS = None
    GET_NAME = None  # Used to create link headers for POST requests
    TTL = 300

    DELETE_SQL: typing.Optional[str] = None
    GET_SQL: typing.Optional[str] = None
    PATCH_SQL: typing.Optional[str] = None
    POST_SQL: typing.Optional[str] = None

    async def prepare(self):
        await super().prepare()
        self.logger.debug('Endpoint: %s', self.NAME)

    def _ensure_keys_are_set(self, kwargs):
        if isinstance(self.ID_KEY, list) \
                and not all(k in kwargs for k in self.ID_KEY):
            raise web.HTTPError(405)
        elif isinstance(self.ID_KEY, str) and self.ID_KEY not in kwargs:
            raise web.HTTPError(405)

    async def delete(self, *args, **kwargs):
        if self.DELETE_SQL is None:
            self.logger.debug('DELETE_SQL not defined')
            raise web.HTTPError(405)
        self._ensure_keys_are_set(kwargs)
        await self._delete(kwargs)

    async def get(self, *args, **kwargs):
        if self.GET_SQL is None:
            self.logger.debug('GET_SQL not defined')
            raise web.HTTPError(405)
        self._ensure_keys_are_set(kwargs)
        if self._respond_with_html:
            return self.render('index.html')
        await self._get(kwargs)

    async def patch(self, *args, **kwargs):
        if self.PATCH_SQL is None:
            self.logger.debug('PATCH_SQL not defined')
            raise web.HTTPError(405)
        self._ensure_keys_are_set(kwargs)
        await self._patch(kwargs)

    async def post(self, *args, **kwargs):
        if self.POST_SQL is None:
            self.logger.debug('POST_SQL not defined')
            raise web.HTTPError(405)
        if isinstance(self.ID_KEY, list) \
                and all(k in kwargs for k in self.ID_KEY):
            raise web.HTTPError(405)
        elif isinstance(self.ID_KEY, str) and self.ID_KEY in kwargs:
            raise web.HTTPError(405)
        await self._post(kwargs)

    def send_response(self, value: dict) -> None:
        """Send the response to the client"""
        self._add_last_modified_header(
            value.get('modified_at', value['created_at']))
        for key in {'created_at', 'modified_at'}:
            if key in value:
                del value[key]

        if isinstance(self.ID_KEY, list):
            args = [str(value[k]) for k in self.ID_KEY]
        else:
            args = [str(value[self.ID_KEY])]
        try:
            self._add_self_link(self.reverse_url(self.NAME, *args))
        except KeyError:
            self.logger.debug('Failed to reverse URL for %s %r',
                              self.NAME, args)
        self._add_link_header()
        super().send_response(value)

    async def _delete(self, kwargs):
        result = await self.postgres_execute(
            self.DELETE_SQL, self._get_query_kwargs(kwargs),
            'delete-{}'.format(self.NAME))
        if not result.row_count:
            raise web.HTTPError(404, reason='Item not found')
        self.set_status(204, reason='Item Deleted')

    async def _get(self, kwargs):
        result = await self.postgres_execute(
            self.GET_SQL, self._get_query_kwargs(kwargs),
            'get-{}'.format(self.NAME))
        if not result.row_count:
            raise web.HTTPError(404, reason='Item not found')
        for key, value in result.row.items():
            if isinstance(value, uuid.UUID):
                result.row[key] = str(value)
        self.send_response(result.row)

    def _get_query_kwargs(self, kwargs) -> dict:
        if isinstance(self.ID_KEY, list):
            return {k: kwargs[k] for k in self.ID_KEY}
        return {self.ID_KEY: kwargs[self.ID_KEY]}

    async def _patch(self, kwargs):
        patch_value = self.get_request_body()

        # Validate the JSONPatch format / payload
        try:
            common.jsonschema_validate('patch.yaml', patch_value, False)
        except ValueError as error:
            self.logger.debug('JSON Schema validation error: %s', error)
            raise web.HTTPError(400, reason=str(error))

        result = await self.postgres_execute(
            self.GET_SQL, self._get_query_kwargs(kwargs),
            'get-{}'.format(self.NAME))
        if not result.row_count:
            raise web.HTTPError(404, reason='Item not found')

        original = dict(result.row)
        for key in {'created_at', 'modified_at'}:
            del original[key]

        for key, value in original.items():
            if isinstance(value, uuid.UUID):
                original[key] = str(value)

        # Apply the patch to the current value
        patch = jsonpatch.JsonPatch(patch_value)
        updated = patch.apply(original)

        # Bail early if there are no changes
        if not {k: original[k] for k in original
                if k in updated and original[k] != updated[k]}:
            self._add_self_link(self.request.path)
            self._add_link_header()
            return self.set_status(304)

        # Validate the new version that will be saved
        try:
            common.jsonschema_validate(self.ITEM_SCHEMA, updated)
        except ValueError as error:
            self.logger.debug('JSON Schema validation error: %s', error)
            raise web.HTTPError(400, reason=str(error))

        if isinstance(self.ID_KEY, list):
            for key in self.ID_KEY:
                updated['current_{}'.format(key)] = kwargs[key]
        else:
            updated['current_{}'.format(self.ID_KEY)] = kwargs[self.ID_KEY]

        LOGGER.debug('Patching %s: %r', self.PATCH_SQL, updated)
        result = await self.postgres_execute(
            self.PATCH_SQL, updated,
            'patch-{}'.format(self.NAME))
        if not result.row_count:
            raise web.HTTPError(500, reason='Failed to update record')

        # Send the new record as a response
        await self._get(self._get_query_kwargs(updated))

    async def _post(self, kwargs) -> None:
        values = self.get_request_body()

        # Handle compound keys for child object CRUD
        if isinstance(self.ID_KEY, list):
            for key in self.ID_KEY:
                if key not in values and key in kwargs:
                    values[key] = kwargs[key]

        # Set defaults of None for all fields in insert
        for name in self.FIELDS:
            if name not in values:
                values[name] = self.DEFAULTS.get(name, None)

        # Validate the record that will be inserted against the schema
        try:
            common.jsonschema_validate(self.ITEM_SCHEMA, values)
        except ValueError as error:
            self.logger.debug('JSON Schema validation error: %s', error)
            raise web.HTTPError(400, reason=str(error))

        result = await self.postgres_execute(
            self.POST_SQL, values, 'post-{}'.format(self.NAME))
        if not result.row_count:
            self.logger.debug('No rows returned')
            raise web.HTTPError(500, reason='Failed to create record')

        # Return the record as if it were a GET
        await self._get(self._get_query_kwargs(result.row))


class ItemsRequestHandler(AuthenticatedRequestHandler):

    GET_SQL = """SELECT * FROM pg_tables WHERE schemaname = 'v1';"""
    TTL = 300

    async def get(self, *args, **kwargs):
        if self._respond_with_html:
            return self.render('index.html')
        result = await self.postgres_execute(
            self.GET_SQL, metric_name='get-{}'.format(self.NAME))
        self.send_response(result.rows)
