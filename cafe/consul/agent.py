from os import getenv
from twisted.internet import defer, reactor, task

from cafe.logging import LoggedObject
from cafe.twisted import async_sleep
from consul.base import Consul as ConsulBase, ConsulException
from consul.std import Consul as ConsulStandardAgent
from consul.twisted import Consul

CONSUL_HOST = getenv('CONSUL_HOST', '127.0.0.1')
CONSUL_PORT = int(getenv('CONSUL_PORT', '8500'))
CONSUL_TOKEN = getenv('CONSUL_TOKEN', None)
CONSUL_SCHEME = getenv('CONSUL_SCHEME', 'http')
CONSUL_DC = getenv('CONSUL_DC', None)
CONSUL_VERIFY = bool(getenv('CONSUL_VERIFY', 'True'))


class SimpleConsulClient(ConsulStandardAgent, object):
    def __init__(self, host=CONSUL_HOST, port=CONSUL_PORT, token=CONSUL_TOKEN, scheme=CONSUL_SCHEME, dc=CONSUL_DC,
                 verify=CONSUL_VERIFY, **kwargs):
        super(SimpleConsulClient, self).__init__(
            host=host, port=port, token=token, scheme=scheme, dc=dc, verify=verify, **kwargs)


class SessionedConsulAgent(LoggedObject, ConsulBase):
    GLOBAL_RETRY_DELAY_SECONDS = int(getenv('CONSUL_GLOBAL_RETRY_DELAY_SECONDS', 10))
    SESSION_TTL_SECONDS = int(getenv('CONSUL_SESSION_TTL_SECONDS', '75'))
    SESSION_HEARTBEAT_SECONDS = int(getenv('CONSUL_SESSION_HEARTBEAT_SECONDS', '75'))
    SESSION_LOCK_DELAY_SECONDS = int(getenv('CONSUL_SESSION_LOCK_DELAY_SECONDS', '15'))
    SESSION_CREATE_RETRY_DELAY_SECONDS = GLOBAL_RETRY_DELAY_SECONDS

    def __init__(self, name, behavior='delete', ttl=None, heartbeat_interval=None, lock_delay=None, host=CONSUL_HOST,
                 port=CONSUL_PORT, token=CONSUL_TOKEN, scheme=CONSUL_SCHEME, dc=CONSUL_DC, verify=CONSUL_VERIFY,
                 **kwargs):
        """
        :type behavior: str
        :param behavior: consul session behavior (release, delete)
        :type ttl: int
        :param ttl: time to live for the session before it is invalidated
        :param name: session name to use
        :type name: str
        :param heartbeat_interval: interval (in seconds) in which a session
            should be renewed, this value is also used as the session ttl.
        :type heartbeat_interval: str
        :type lock_delay: int
        :param lock_delay: consul lock delay to use for sessions
        """
        assert behavior in ('release', 'delete')
        self.name = name
        self.ttl = ttl or self.SESSION_TTL_SECONDS
        self.heartbeat_interval = heartbeat_interval or self.SESSION_HEARTBEAT_SECONDS
        self.lock_delay = lock_delay or self.SESSION_LOCK_DELAY_SECONDS
        if 0 > self.lock_delay > 60:
            self.logger.debug('invalid lock-delay=%s specified, using defaults', self.lock_delay)
            self.lock_delay = 15
        self.consul = Consul(host=host, port=port, token=token, scheme=scheme, dc=dc, verify=verify, **kwargs)
        self.session_id = None
        self.heartbeat = task.LoopingCall(self.session_renew)
        reactor.callLater(0, self.session_create)
        self.start()
        reactor.addSystemEventTrigger('before', 'shutdown', self.stop)
        reactor.addSystemEventTrigger('before', 'shutdown', self.session_destroy)

    @property
    def agent(self):
        return self.consul

    def __getattr__(self, item):
        """
        :type item: str
        :rtype: consul.base.Consul
        """
        return getattr(self.agent, item)

    def start(self):
        """
        Start this instance.
        """
        self.logger.trace('starting consul agent')

    def stop(self):
        """
        Execute clean-up tasks.
        """
        self.logger.trace('stopping consul agent')

    @property
    def ready(self):
        """Check if a session has been established with consul."""
        return self.session_id is not None

    @defer.inlineCallbacks
    def wait_for_ready(self, attempts=None, interval=None):
        """
        :param attempts: number of attempts before giving up, if None there is
            no giving up.
        :type attempts: int or None
        :param interval: interval (in seconds), by default the create retry interval is used
        :type interval: int or None
        """
        interval = interval if interval is not None else self.SESSION_CREATE_RETRY_DELAY_SECONDS
        attempt = 0
        while not self.ready and (attempts is None or attempt <= attempts):
            attempt += 1
            self.logger.debug('attempt=%s interval=%ss waiting for session to established', attempt, interval)
            yield async_sleep(interval)

    @defer.inlineCallbacks
    def session_create(self, retry=True):
        """
        Create a session, and set the internal `session_id` property. If an
        exception is encountered during creation, the operation will be
        reattempted again at half the ttl of the session itself if `retry` is
        `True`.

        :param retry: retry later if creation fails
        :type retry: bool
        """
        try:
            self.logger.trace('attempting to create a new session')
            self.session_id = yield self.consul.session.create(
                self.name, behavior='delete', ttl=self.ttl, lock_delay=self.lock_delay)
            self.logger.info('name=%s session=%s created', self.name, self.session_id)

            if not self.heartbeat.running:
                reactor.callLater(0, self.heartbeat.start, interval=self.heartbeat_interval)
        except ConsulException as e:
            self.logger.warning(
                'session=%s creation failed, retrying reason=%s',
                self.session_id, e.message)
            if retry:
                # try again in SESSION_CREATE_RETRY_DELAY_SECONDS
                reactor.callLater(self.SESSION_CREATE_RETRY_DELAY_SECONDS, self.session_create)

    @defer.inlineCallbacks
    def session_renew(self):
        """Renew session if one is active, else do nothing."""
        try:
            if self.session_id is not None:
                self.logger.trace('name=%s session=%s renewing session', self.name, self.session_id)
                yield self.consul.session.renew(self.session_id)
        except ConsulException as e:
            self.logger.warning(
                'session=%s renewal attempt failed reason=%s',
                self.session_id, e.message
            )

    @defer.inlineCallbacks
    def session_destroy(self):
        """Destroy a session if one is active, else do nothing."""
        try:
            if self.session_id is not None:
                if self.heartbeat.running:
                    self.logger.trace('name=%s session=%s stopping heartbeat', self.name, self.session_id)
                    self.heartbeat.stop()

                self.logger.trace('name=%s session=%s destroying session', self.name, self.session_id)
                yield self.consul.session.destroy(self.session_id)
                self.logger.info('name=%s session=%s destroyed session', self.name, self.session_id)
                self.session_id = None
        except ConsulException as e:
            self.logger.warning(
                'session=%s destruction attempt failed reason=%s',
                self.session_id, e.message
            )

    @classmethod
    def create_lock_key(cls, *args):
        """Helper method to create a valid key provider components as args"""
        return '/'.join(args)

    @defer.inlineCallbacks
    def _lock(self, action, key, value='', **kwargs):
        """
        Internal method to acquire/release a lock

        :type key: str
        :type value: str
        """
        assert action in ('acquire', 'release')
        self.logger.debug(
            'lock=%s action=%s session=%s value=%s',
            key, action, self.session_id, value
        )
        if not self.ready:
            self.logger.trace(
                'lock=%s action=%s failed as consul agent is not ready',
                key, action
            )
            result = False
        else:
            kwargs[action] = self.session_id
            result = yield self.consul.kv.put(key=key, value=value, **kwargs)
        self.logger.info('lock=%s action=%s result=%s', key, action, result)
        defer.returnValue(result)

    @defer.inlineCallbacks
    def acquire_lock(self, key, value='', **kwargs):
        """
        Acquire a lock with a provided value.

        :type key: str
        :type value: str
        """
        result = yield self._lock(action='acquire', key=key, value=value, **kwargs)
        defer.returnValue(result)

    @defer.inlineCallbacks
    def release_lock(self, key, value='', delete=False, **kwargs):
        """
        Release a lock with a provided value.

        :type key: str
        :type value: str
        :type delete: bool
        """
        result = yield self._lock(action='release', key=key, value=value, **kwargs)
        if result and delete:
            try:
                self.logger.trace('key=%s deleting as lock is released', key)
                yield self.consul.kv.delete(key=key)
            except ConsulException as e:
                self.logger.warning(
                    'key=%s failed to delete reason=%s', key, e.message)
        defer.returnValue(result)

    @defer.inlineCallbacks
    def wait_for_lock(self, key, value='', attempts=None, **kwargs):
        """
        Wait till a lock is acquired. If attempts is None, wait for ever.

        :type key: str
        :type value: str
        :type attempts: None or int
        :rtype: bool
        """
        index = None
        result = False
        yield self.wait_for_ready()
        while not result and (attempts is None or attempts >= 0):
            self.logger.debug(
                'lock=%s waiting for lock; %s attempts left',
                key, attempts if attempts is not None else 'infinite')
            result = yield self.acquire_lock(key=key, value=value, **kwargs)
            if not result:
                index, _ = yield self.agent.kv.get(key=key, index=index)
                if attempts is not None:
                    attempts -= 1
        defer.returnValue(result)


class DistributedConsulAgent(SessionedConsulAgent):
    ELECTION_EXPIRY = int(getenv('CONSUL_ELECTION_EXPIRY', SessionedConsulAgent.SESSION_HEARTBEAT_SECONDS))
    ELECTION_RETRY = int(getenv('CONSUL_ELECTION_RETRY', SessionedConsulAgent.SESSION_CREATE_RETRY_DELAY_SECONDS))

    def __init__(self, name, behavior='delete', ttl=None, heartbeat_interval=None, lock_delay=None, host=CONSUL_HOST,
                 port=CONSUL_PORT, token=CONSUL_TOKEN, scheme=CONSUL_SCHEME, dc=CONSUL_DC, verify=CONSUL_VERIFY,
                 **kwargs):
        super(DistributedConsulAgent, self).__init__(
            name, behavior=behavior, ttl=ttl, heartbeat_interval=heartbeat_interval, lock_delay=lock_delay,
            host=host, port=port, token=token, scheme=scheme, dc=dc, verify=verify, **kwargs
        )
        self._leader = None
        self.is_leader = False
        self._abstain = False
        self.leader_key = 'service/{}/leader'.format(name)
        reactor.callLater(0, self.update_leader)

    @property
    def leader(self):
        """Current leader data"""
        return self._leader

    @leader.setter
    def leader(self, value):
        self._leader = value
        if value is None:
            # immediate retry if we are the leader
            reactor.callLater(0, self.acquire_leadership)

    @defer.inlineCallbacks
    def update_leader(self, index=None):
        try:
            index, data = yield self.agent.kv.get(key=self.leader_key, index=index)
            if data is not None and hasattr(data, 'get'):
                self.leader = data.get('Value', None)
            else:
                # the key does not exist, we are using 'delete' behaviour
                self.leader = None
            self.logger.trace('name=%s session=%s leader=%s', self.name, self.session_id, self.leader)
        except ConsulException as e:
            self.logger.error(
                'leader update failed, retrying later exception=%s message=%s', e.__class__.__name__, e.message)
            yield async_sleep(self.SESSION_CREATE_RETRY_DELAY_SECONDS)
        reactor.callLater(0, self.update_leader, index=index)

    @property
    def candidate_data(self):
        """
        Data to use when applying for leadership.

        :rtype: str
        """
        return self.session_id

    @defer.inlineCallbacks
    def acquire_leadership(self):
        """
        Try to acquire leadership.

        :rtype: bool
        """
        if self.session_id is None:
            self.logger.trace('name=%s session not ready, retrying later', self.name)
            reactor.callLater(self.ELECTION_RETRY, self.acquire_leadership)
        elif self._abstain:
            self.logger.trace('name=%s session=%s currently abstaining from elections, skipping', self.name,
                              self.session_id)
        elif self.leader is not None:
            self.logger.trace('name=%s leader exists, skipping', self.name)
        else:
            value = self.candidate_data
            self.logger.trace('name=%s session=%s can i haz leadership', self.name, self.session_id)
            try:
                self.is_leader = yield self.acquire_lock(key=self.leader_key, value=value)
                if self.is_leader:
                    self.logger.info('name=%s session=%s acquired leadership', self.name, self.session_id)
                else:
                    # handle consul lock-delay safe guard, retry a bit later
                    reactor.callLater(self.ELECTION_RETRY, self.acquire_leadership)
                self.logger.trace('name=%s session=%s acquired_leadership=%s', self.name, self.session_id,
                                  self.is_leader)
            except ConsulException as e:
                self.logger.trace('name=%s session=%s acquiring leadership attempt failed reason=%s', self.name,
                                  self.session_id, e.message)
        defer.returnValue(self.is_leader)

    @defer.inlineCallbacks
    def relinquish_leadership(self, abstain=False):
        """
        :param abstain: abstain from next election till a new leader is elected,
            WARNING: be sure you know what you are doing, this can lead to potential deadlocks.
        :type abstain: bool
        """
        try:
            self.logger.info('name=%s session=%s relinquishing leadership', self.name, self.session_id)
            self._abstain = abstain
            yield self.release_lock(key=self.leader_key)
            if abstain:
                self.logger.debug('name=%s session=%s waiting for next leader', self.name, self.session_id)
                yield self.wait_for_leader()
        finally:
            self._abstain = False

    @defer.inlineCallbacks
    def wait_for_leader(self, attempts=None, interval=None):
        """
        :param attempts: number of attempts before giving up, if None there is
            no giving up.
        :type attempts: int or None
        :param interval: interval (in seconds), by default the election retry interval is used
        :type interval: int or None
        """
        yield self.wait_for_ready()
        interval = interval if interval is not None else self.ELECTION_RETRY
        attempt = 0
        while self.leader is None and (attempts is None or attempt <= attempts):
            attempt += 1
            self.logger.debug('attempt=%s interval=%ss waiting for leader to be elected', attempt, interval)
            yield async_sleep(interval)

    @defer.inlineCallbacks
    def wait_for_leadership(self):
        yield self.wait_for_leader()
        while not self.is_leader:
            yield async_sleep(self.ELECTION_EXPIRY)