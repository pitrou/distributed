from __future__ import print_function, division, absolute_import

from collections import defaultdict
from datetime import timedelta
from functools import partial
import logging
import six
import socket
import struct
import sys
import traceback
import uuid

from six import string_types

from toolz import assoc, first

try:
    import cPickle as pickle
except ImportError:
    import pickle
import cloudpickle
from tornado import gen
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.locks import Event

from .comm import connect, listen, CommClosedError
from .comm.core import parse_address, normalize_address, Comm
from .comm.utils import parse_host_port, unparse_host_port
from .compatibility import PY3, unicode, WINDOWS
from .config import config
from .metrics import time
from .system_monitor import SystemMonitor
from .utils import get_ip, get_traceback, truncate_exception, ignoring, shutting_down
from . import protocol


class RPCClosed(IOError):
    pass


logger = logging.getLogger(__name__)


def get_total_physical_memory():
    try:
        import psutil
        return psutil.virtual_memory().total / 2
    except ImportError:
        return 2e9


MAX_BUFFER_SIZE = get_total_physical_memory()


def handle_signal(sig, frame):
    IOLoop.instance().add_callback(IOLoop.instance().stop)


class Server(object):
    """ Distributed TCP Server

    Superclass for both Worker and Scheduler objects.
    Inherits from ``tornado.tcpserver.TCPServer``, adding a protocol for RPC.

    **Handlers**

    Servers define operations with a ``handlers`` dict mapping operation names
    to functions.  The first argument of a handler function must be a stream for
    the connection to the client.  Other arguments will receive inputs from the
    keys of the incoming message which will always be a dictionary.

    >>> def pingpong(stream):
    ...     return b'pong'

    >>> def add(stream, x, y):
    ...     return x + y

    >>> handlers = {'ping': pingpong, 'add': add}
    >>> server = Server(handlers)  # doctest: +SKIP
    >>> server.listen(8000)  # doctest: +SKIP

    **Message Format**

    The server expects messages to be dictionaries with a special key, `'op'`
    that corresponds to the name of the operation, and other key-value pairs as
    required by the function.

    So in the example above the following would be good messages.

    *  ``{'op': 'ping'}``
    *  ``{'op': 'add': 'x': 10, 'y': 20}``
    """
    default_ip = ''
    default_port = 0

    def __init__(self, handlers, connection_limit=512, deserialize=True,
                 io_loop=None):
        self.handlers = assoc(handlers, 'identity', self.identity)
        self.id = str(uuid.uuid1())
        self._address = None
        self._listen_address = None
        self._port = None
        self._comms = {}
        self.rpc = ConnectionPool(limit=connection_limit,
                                  deserialize=deserialize)
        self.deserialize = deserialize
        self.monitor = SystemMonitor()
        self.counters = None
        self.digests = None

        self.listener = None
        self.io_loop = io_loop or IOLoop.current()

        if hasattr(self, 'loop'):
            # XXX?
            with ignoring(ImportError):
                from .counter import Digest
                self.digests = defaultdict(partial(Digest, loop=self.loop))

            from .counter import Counter
            self.counters = defaultdict(partial(Counter, loop=self.loop))

            pc = PeriodicCallback(self.monitor.update, 500, io_loop=self.loop)
            self.loop.add_callback(pc.start)
            if self.digests is not None:
                self._last_tick = time()
                self._tick_pc = PeriodicCallback(self._measure_tick, 20, io_loop=self.loop)
                self.loop.add_callback(self._tick_pc.start)

        self.__stopped = False

    def stop(self):
        if not self.__stopped:
            self.__stopped = True
            if self.listener is not None:
                self.listener.stop()

    def _measure_tick(self):
        now = time()
        self.digests['tick-duration'].add(now - self._last_tick)
        self._last_tick = now

    @property
    def address(self):
        """
        The address this Server can be contacted on.
        """
        if not self._address:
            if self.listener is None:
                raise ValueError("cannot get address of non-running Server")
            self._address = self.listener.contact_address
        return self._address

    @property
    def listen_address(self):
        """
        The address this Server is listening on.  This may be a wildcard
        address such as `tcp://0.0.0.0:1234`.
        """
        if not self._listen_address:
            if self.listener is None:
                raise ValueError("cannot get listen address of non-running Server")
            self._listen_address = self.listener.listen_address
        return self._listen_address

    @property
    def port(self):
        """
        The port number this Server is listening on.
        """
        if not self._port:
            _, loc = parse_address(self.address)
            _, self._port = parse_host_port(loc)
        return self._port

    def identity(self, comm):
        return {'type': type(self).__name__, 'id': self.id}

    def listen(self, port_or_addr=None):
        if port_or_addr is None:
            port_or_addr = self.default_port
        if isinstance(port_or_addr, int):
            addr = unparse_host_port(self.default_ip, port_or_addr)
        elif isinstance(port_or_addr, tuple):
            addr = unparse_host_port(*port_or_addr)
        else:
            addr = port_or_addr
            assert isinstance(addr, string_types)
        self.listener = listen(addr, self.handle_comm,
                               deserialize=self.deserialize)
        self.listener.start()

    @gen.coroutine
    def handle_comm(self, comm, shutting_down=shutting_down):
        """ Dispatch new communications to coroutine-handlers

        Handlers is a dictionary mapping operation names to functions or
        coroutines.

            {'get_data': get_data,
             'ping': pingpong}

        Coroutines should expect a single Comm object.
        """
        address = comm.peer_address
        op = None

        logger.debug("Connection from %r to %s", address, type(self).__name__)
        self._comms[comm] = op
        try:
            while True:
                try:
                    msg = yield comm.read()
                    logger.debug("Message from %r: %s", address, msg)
                except EnvironmentError as e:
                    if not shutting_down():
                        logger.debug("Lost connection to %r while reading message: %s."
                                    " Last operation: %s",
                                    address, e, op)
                    break
                except Exception as e:
                    logger.exception(e)
                    yield comm.write(error_message(e, status='uncaught-error'))
                    continue
                if not isinstance(msg, dict):
                    raise TypeError("Bad message type.  Expected dict, got\n  "
                                    + str(msg))
                op = msg.pop('op')
                if self.counters is not None:
                    self.counters['op'].add(op)
                self._comms[comm] = op
                close_desired = msg.pop('close', False)
                reply = msg.pop('reply', True)
                if op == 'close':
                    if reply:
                        yield comm.write('OK')
                    break
                try:
                    handler = self.handlers[op]
                except KeyError:
                    result = "No handler found: %s" % op
                    logger.warn(result, exc_info=True)
                else:
                    logger.debug("Calling into handler %s", handler.__name__)
                    try:
                        result = handler(comm, **msg)
                        if type(result) is gen.Future:
                            result = yield result
                    except CommClosedError as e:
                        logger.warn("Lost connection to %r: %s", address, e)
                        break
                    except Exception as e:
                        logger.exception(e)
                        result = error_message(e, status='uncaught-error')
                if reply and result != 'dont-reply':
                    try:
                        yield comm.write(result)
                    except EnvironmentError as e:
                        logger.warn("Lost connection to %r while sending result for op %r: %s",
                                    address, op, e)
                        break
                if close_desired:
                    yield comm.close()
                if comm.closed():
                    break

        finally:
            del self._comms[comm]
            if not shutting_down() and not comm.closed():
                try:
                    comm.abort()
                except Exception as e:
                    logger.error("Failed while closing connection to %r: %s",
                                 address, e)


def pingpong(comm):
    return b'pong'


@gen.coroutine
def send_recv(comm=None, addr=None, reply=True, deserialize=True, **kwargs):
    """ Send and recv with a stream

    Keyword arguments turn into the message

    response = yield send_recv(stream, op='ping', reply=True)
    """
    assert (comm is None) != (addr is None)
    if comm is None:
        comm = yield connect(addr_from_args(addr), deserialize=deserialize)

    msg = kwargs
    msg['reply'] = reply
    please_close = kwargs.get('close')

    try:
        yield comm.write(msg)
        if reply:
            response = yield comm.read()
        else:
            response = None
    except EnvironmentError:
        # On communication errors, we should simply close the communication
        please_close = True
        raise
    finally:
        if please_close:
            yield comm.close()

    if isinstance(response, dict) and response.get('status') == 'uncaught-error':
        six.reraise(*clean_exception(**response))
    raise gen.Return(response)


def addr_from_args(addr=None, ip=None, port=None):
    if addr is None:
        addr = (ip, port)
    else:
        assert ip is None and port is None
    if isinstance(addr, tuple):
        addr = unparse_host_port(*addr)
    return normalize_address(addr)


class rpc(object):
    """ Conveniently interact with a remote server

    >>> remote = rpc(ip=ip, port=port)  # doctest: +SKIP
    >>> response = yield remote.add(x=10, y=20)  # doctest: +SKIP

    One rpc object can be reused for several interactions.
    Additionally, this object creates and destroys many streams as necessary
    and so is safe to use in multiple overlapping communications.

    When done, close comms explicitly.

    >>> remote.close_comms()  # doctest: +SKIP
    """
    active = 0
    comms = ()
    address = None

    def __init__(self, arg=None, comm=None, deserialize=True, timeout=3):
        self.comms = {}
        self.address = normalize_address(coerce_to_address(arg))
        self.ip, self.port = parse_host_port(parse_address(self.address)[1])
        self.timeout = timeout
        self.status = 'running'
        self.deserialize = deserialize
        rpc.active += 1

    @gen.coroutine
    def live_comm(self):
        """ Get an open communication

        Some comms to the ip/port target may be in current use by other
        coroutines.  We track this with the `comms` dict

            :: {comm: True/False if open and ready for use}

        This function produces an open communication, either by taking one
        that we've already made or making a new one if they are all taken.
        This also removes comms that have been closed.

        When the caller is done with the stream they should set

            self.comms[comm] = True

        As is done in __getattr__ below.
        """
        if self.status == 'closed':
            raise RPCClosed("RPC Closed")
        to_clear = set()
        open = False
        for comm, open in self.comms.items():
            if comm.closed():
                to_clear.add(comm)
            if open:
                break
        for s in to_clear:
            del self.comms[s]
        if not open or comm.closed():
            comm = yield connect(self.address, self.timeout,
                                 deserialize=self.deserialize)
        self.comms[comm] = False     # mark as taken
        raise gen.Return(comm)

    def close_comms(self):

        @gen.coroutine
        def _close_comm(comm):
            # Make sure we tell the peer to close
            try:
                yield comm.write({'op': 'close', 'reply': False})
                yield comm.close()
            except EnvironmentError:
                comm.abort()

        for comm in list(self.comms):
            if comm and not comm.closed():
                _close_comm(comm)
        self.comms.clear()

    def __getattr__(self, key):
        @gen.coroutine
        def send_recv_from_rpc(**kwargs):
            try:
                comm = yield self.live_comm()
                result = yield send_recv(comm=comm, op=key, **kwargs)
            except (RPCClosed, CommClosedError) as e:
                raise e.__class__("%s: while trying to call remote method %r"
                                  % (e, key,))

            self.comms[comm] = True  # mark as open
            raise gen.Return(result)
        return send_recv_from_rpc

    def close_rpc(self):
        if self.status != 'closed':
            rpc.active -= 1
        self.status = 'closed'
        self.close_comms()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close_rpc()

    def __del__(self):
        if self.status != 'closed':
            rpc.active -= 1
            self.status = 'closed'
            still_open = [comm for comm in self.comms if not comm.closed()]
            if still_open:
                logger.warn("rpc object %s deleted with %d open comms",
                            self, len(still_open))
                for comm in still_open:
                    comm.abort()

    def __repr__(self):
        return "<rpc to %r, %d comms>" % (self.address, len(self.comms))


class PooledRPCCall(object):
    """ The result of ConnectionPool()('host:port')

    See Also:
        ConnectionPool
    """
    def __init__(self, addr, pool):
        self.addr = addr
        self.pool = pool

    def __getattr__(self, key):
        @gen.coroutine
        def send_recv_from_rpc(**kwargs):
            comm = yield self.pool.connect(self.addr)
            try:
                result = yield send_recv(comm=comm, op=key,
                        deserialize=self.pool.deserialize, **kwargs)
            finally:
                self.pool.reuse(self.addr, comm)

            raise gen.Return(result)
        return send_recv_from_rpc

    def close_rpc(self):
        pass

    def __repr__(self):
        return "<pooled rpc to %r>" % (self.addr,)


class ConnectionPool(object):
    """ A maximum sized pool of Comm objects.

    This provides a connect method that mirrors the normal distributed.connect
    method, but provides connection sharing and tracks connection limits.

    This object provides an ``rpc`` like interface::

        >>> rpc = ConnectionPool(limit=512)
        >>> scheduler = rpc('127.0.0.1:8786')
        >>> workers = [rpc(ip=ip, port=port) for ip, port in ...]

        >>> info = yield scheduler.identity()

    It creates enough comms to satisfy concurrent connections to any
    particular address::

        >>> a, b = yield [scheduler.who_has(), scheduler.has_what()]

    It reuses existing streams so that we don't have to continuously reconnect.

    It also maintains a comm limit to avoid "too many open file handle"
    issues.  Whenever this maximum is reached we clear out all idling comms.
    If that doesn't do the trick then we wait until one of the occupied comms
    closes.

    Parameters
    ----------
    limit: int
        The number of open comms to maintain at once
    deserialize: bool
        Whether or not to deserialize data by default or pass it through
    """
    def __init__(self, limit=512, deserialize=True):
        # Total number of open comms
        self.open = 0
        # Number of comms currently in use
        self.active = 0
        # Max number of open comms
        self.limit = limit
        # Number of comms in available == open - active
        self.available = defaultdict(set)
        # Number of comms in occupied == active
        self.occupied = defaultdict(set)
        self.deserialize = deserialize
        self.event = Event()

    def __str__(self):
        return "<ConnectionPool: open=%d, active=%d>" % (self.open,
                self.active)

    __repr__ = __str__

    def __call__(self, addr=None, ip=None, port=None):
        """ Cached rpc objects """
        addr = addr_from_args(addr=addr, ip=ip, port=port)
        return PooledRPCCall(addr, self)

    @gen.coroutine
    def connect(self, addr, timeout=3):
        """
        Get a Comm to the given address.  For internal use.
        """
        available = self.available[addr]
        occupied = self.occupied[addr]
        if available:
            comm = available.pop()
            if not comm.closed():
                self.active += 1
                occupied.add(comm)
                raise gen.Return(comm)
            else:
                self.open -= 1

        while self.open >= self.limit:
            self.event.clear()
            self.collect()
            yield self.event.wait()

        self.open += 1
        try:
            comm = yield connect(addr, timeout=timeout,
                                 deserialize=self.deserialize)
        except Exception:
            self.open -= 1
            raise
        self.active += 1
        occupied.add(comm)

        if self.open >= self.limit:
            self.event.clear()

        raise gen.Return(comm)

    def reuse(self, addr, comm):
        """
        Reuse an open communication to the given address.  For internal use.
        """
        self.occupied[addr].remove(comm)
        self.active -= 1
        if comm.closed():
            self.open -= 1
            if self.open < self.limit:
                self.event.set()
        else:
            self.available[addr].add(comm)

    def collect(self):
        """
        Collect open but unused communications, to allow opening other ones.
        """
        logger.info("Collecting unused comms.  open: %d, active: %d",
                    self.open, self.active)
        for addr, comms in self.available.items():
            for comm in comms:
                comm.close()
            comms.clear()
        self.open = self.active
        if self.open < self.limit:
            self.event.set()

    def close(self):
        """
        Close all communications abruptly.
        """
        for comms in self.available.values():
            for comm in comms:
                comm.abort()
        for comms in self.occupied.values():
            for comm in comms:
                comm.abort()


def coerce_to_address(o):
    if isinstance(o, (list, tuple)):
        o = tuple(o)
        if isinstance(o[0], bytes):
            o = (o[0].decode(),) + o[1:]
        o = unparse_host_port(*o)

    return o


def error_message(e, status='error'):
    """ Produce message to send back given an exception has occurred

    This does the following:

    1.  Gets the traceback
    2.  Truncates the exception and the traceback
    3.  Serializes the exception and traceback or
    4.  If they can't be serialized send string versions
    5.  Format a message and return

    See Also
    --------
    clean_exception: deserialize and unpack message into exception/traceback
    six.reraise: raise exception/traceback
    """
    tb = get_traceback()
    e2 = truncate_exception(e, 1000)
    try:
        e3 = protocol.pickle.dumps(e2)
        protocol.pickle.loads(e3)
    except Exception:
        e3 = Exception(str(e2))
        e3 = protocol.pickle.dumps(e3)
    try:
        tb2 = protocol.pickle.dumps(tb)
    except Exception:
        tb2 = ''.join(traceback.format_tb(tb))
        tb2 = protocol.pickle.dumps(tb2)

    if len(tb2) > 10000:
        tb2 = None

    return {'status': status, 'exception': e3, 'traceback': tb2}


def clean_exception(exception, traceback, **kwargs):
    """ Reraise exception and traceback. Deserialize if necessary

    See Also
    --------
    error_message: create and serialize errors into message
    """
    if isinstance(exception, bytes):
        exception = protocol.pickle.loads(exception)
    if isinstance(traceback, bytes):
        traceback = protocol.pickle.loads(traceback)
    elif isinstance(traceback, string_types):
        traceback = None  # why?
    return type(exception), exception, traceback
