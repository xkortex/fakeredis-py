import asyncio
import re

import pytest
import pytest_asyncio
import redis
from packaging.version import Version

aioredis = pytest.importorskip("aioredis", minversion='2.0.0a1')
import async_timeout

import fakeredis.aioredis

REDIS_VERSION = Version(redis.__version__)

redis_4_2_and_above = pytest.mark.skipif(
    REDIS_VERSION < Version('4.2.0'),
    reason="Test is only applicable to redis-py 4.2.0 and above"
)

pytestmark = [
    pytest.mark.asyncio,
]
fake_only = pytest.mark.parametrize(
    'r',
    [pytest.param('fake', marks=pytest.mark.fake)],
    indirect=True
)
import importlib

lua_module = importlib.util.find_spec("lupa")
lupa_required_for_test = pytest.mark.skipif(
    lua_module is None,
    reason="Test is only applicable if lupa is installed"
)


@pytest_asyncio.fixture(
    params=[
        pytest.param('fake', marks=pytest.mark.fake),
        pytest.param('real', marks=pytest.mark.real)
    ]
)
async def r(request):
    if request.param == 'fake':
        fake_server = request.getfixturevalue('fake_server')
        ret = fakeredis.aioredis.FakeRedis(server=fake_server)
    else:
        if not request.getfixturevalue('is_redis_running'):
            pytest.skip('Redis is not running')
        ret = aioredis.Redis()
        fake_server = None
    if not fake_server or fake_server.connected:
        await ret.flushall()

    yield ret

    if not fake_server or fake_server.connected:
        await ret.flushall()
    await ret.connection_pool.disconnect()


@pytest_asyncio.fixture
async def conn(r):
    """A single connection, rather than a pool."""
    async with r.client() as conn:
        yield conn


@redis_4_2_and_above
def test_redis_asyncio_is_used():
    """Redis 4.2+ has support for asyncio and should be preferred over aioredis"""
    assert not hasattr(fakeredis.aioredis, "__version__")


async def test_ping(r):
    pong = await r.ping()
    assert pong is True


async def test_types(r):
    await r.hset('hash', mapping={'key1': 'value1', 'key2': 'value2', 'key3': 123})
    result = await r.hgetall('hash')
    assert result == {
        b'key1': b'value1',
        b'key2': b'value2',
        b'key3': b'123'
    }


async def test_transaction(r):
    async with r.pipeline(transaction=True) as tr:
        tr.set('key1', 'value1')
        tr.set('key2', 'value2')
        ok1, ok2 = await tr.execute()
    assert ok1
    assert ok2
    result = await r.get('key1')
    assert result == b'value1'


async def test_transaction_fail(r):
    await r.set('foo', '1')
    async with r.pipeline(transaction=True) as tr:
        await tr.watch('foo')
        await r.set('foo', '2')  # Different connection
        tr.multi()
        tr.get('foo')
        with pytest.raises(aioredis.exceptions.WatchError):
            await tr.execute()


async def test_pubsub(r, event_loop):
    queue = asyncio.Queue()

    async def reader(ps):
        while True:
            message = await ps.get_message(ignore_subscribe_messages=True, timeout=5)
            if message is not None:
                if message.get('data') == b'stop':
                    break
                queue.put_nowait(message)

    async with async_timeout.timeout(5), r.pubsub() as ps:
        await ps.subscribe('channel')
        task = event_loop.create_task(reader(ps))
        await r.publish('channel', 'message1')
        await r.publish('channel', 'message2')
        result1 = await queue.get()
        result2 = await queue.get()
        assert result1 == {
            'channel': b'channel',
            'pattern': None,
            'type': 'message',
            'data': b'message1'
        }
        assert result2 == {
            'channel': b'channel',
            'pattern': None,
            'type': 'message',
            'data': b'message2'
        }
        await r.publish('channel', 'stop')
        await task


@pytest.mark.slow
async def test_pubsub_timeout(r):
    async with r.pubsub() as ps:
        await ps.subscribe('channel')
        await ps.get_message(timeout=0.5)  # Subscription message
        message = await ps.get_message(timeout=0.5)
        assert message is None


@pytest.mark.slow
async def test_pubsub_disconnect(r):
    async with r.pubsub() as ps:
        await ps.subscribe('channel')
        await ps.connection.disconnect()
        message = await ps.get_message(timeout=0.5)  # Subscription message
        assert message is not None
        message = await ps.get_message(timeout=0.5)
        assert message is None


async def test_blocking_ready(r, conn):
    """Blocking command which does not need to block."""
    await r.rpush('list', 'x')
    result = await conn.blpop('list', timeout=1)
    assert result == (b'list', b'x')


@pytest.mark.slow
async def test_blocking_timeout(conn):
    """Blocking command that times out without completing."""
    result = await conn.blpop('missing', timeout=1)
    assert result is None


@pytest.mark.slow
async def test_blocking_unblock(r, conn, event_loop):
    """Blocking command that gets unblocked after some time."""

    async def unblock():
        await asyncio.sleep(0.1)
        await r.rpush('list', 'y')

    task = event_loop.create_task(unblock())
    result = await conn.blpop('list', timeout=1)
    assert result == (b'list', b'y')
    await task


async def test_wrongtype_error(r):
    await r.set('foo', 'bar')
    with pytest.raises(aioredis.ResponseError, match='^WRONGTYPE'):
        await r.rpush('foo', 'baz')


async def test_syntax_error(r):
    with pytest.raises(aioredis.ResponseError,
                       match="^wrong number of arguments for 'get' command$"):
        await r.execute_command('get')


async def test_no_script_error(r):
    with pytest.raises(aioredis.exceptions.NoScriptError):
        await r.evalsha('0123456789abcdef0123456789abcdef', 0)


@lupa_required_for_test
async def test_failed_script_error(r):
    await r.set('foo', 'bar')
    with pytest.raises(aioredis.ResponseError, match='^Error running script'):
        await r.eval('return redis.call("ZCOUNT", KEYS[1])', 1, 'foo')


@fake_only
def test_repr(r):
    assert re.fullmatch(
        r'ConnectionPool<FakeConnection<server=<fakeredis._server.FakeServer object at .*>,db=0>>',
        repr(r.connection_pool)
    )


@fake_only
@pytest.mark.disconnected
async def test_not_connected(r):
    with pytest.raises(aioredis.ConnectionError):
        await r.ping()


@fake_only
async def test_disconnect_server(r, fake_server):
    await r.ping()
    fake_server.connected = False
    with pytest.raises(aioredis.ConnectionError):
        await r.ping()
    fake_server.connected = True


@pytest.mark.fake
async def test_from_url():
    r0 = fakeredis.aioredis.FakeRedis.from_url('redis://localhost?db=0')
    r1 = fakeredis.aioredis.FakeRedis.from_url('redis://localhost?db=1')
    # Check that they are indeed different databases
    await r0.set('foo', 'a')
    await r1.set('foo', 'b')
    assert await r0.get('foo') == b'a'
    assert await r1.get('foo') == b'b'
    await r0.connection_pool.disconnect()
    await r1.connection_pool.disconnect()


@fake_only
async def test_from_url_with_server(r, fake_server):
    r2 = fakeredis.aioredis.FakeRedis.from_url('redis://localhost', server=fake_server)
    await r.set('foo', 'bar')
    assert await r2.get('foo') == b'bar'
    await r2.connection_pool.disconnect()


@pytest.mark.fake
async def test_without_server():
    r = fakeredis.aioredis.FakeRedis()
    assert await r.ping()


@pytest.mark.fake
async def test_without_server_disconnected():
    r = fakeredis.aioredis.FakeRedis(connected=False)
    with pytest.raises(aioredis.ConnectionError):
        await r.ping()
