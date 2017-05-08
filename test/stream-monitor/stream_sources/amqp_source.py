"""
Copyright (c) 2017 Dell Inc. or its subsidiaries. All Rights Reserved.

A set of super-simple matchers to use to self-test the matching framework.
"""
from gevent import monkey
monkey.patch_dns()
monkey.patch_time()
monkey.patch_builtins()
monkey.patch_select()
import re
import sys
import time
import optparse
import uuid
import gevent
import gevent.queue
import json
from pexpect import EOF
from datetime import datetime
from .monitor_abc import StreamMonitorBaseClass
from .stream_matchers_base import StreamMatchBase
from .stream_matchers_results import StreamRunResults
from .amqp_od import RackHDAMQPOnDemand
from .ssh_helper import SSHHelper
from kombu import Connection, Producer, Queue, Exchange, Consumer


class _AMQPServerWrapper(object):
    def __init__(self, amqp_url, logs):
        self.__logs = logs
        self.__connection = Connection(amqp_url)
        self.__connection.connect()
        self.__monitors = {}
        self.__running = True
        self.__consumer = Consumer(self.__connection)
        self.__consumer_gl = gevent.spawn(self.__consumer_greenlet_main)
        self.__consumer_gl.greenlet_name = 'amqp-consumer-gl'  # allowing flogging to print a nice name
        gevent.sleep(0.0)

    def __consumer_greenlet_main(self):
        gevent.sleep(0)
        self.__consumer.consume()
        while self.__running:
            try:
                self.__connection.drain_events(timeout=0.5)
            except Exception as ex:     # NOQA: assigned but not used (left in for super-duper-low-level-debug)
                # print("was woken because {}".format(ex))
                pass
            gevent.sleep(0.1)  # make -sure- to yield cpu...
            # print("---loop")

    def __on_message_cb(self, msg):
        self.__logs.idl.debug('Inbound AMQP msg: %s', msg)
        ct = msg.delivery_info['consumer_tag']
        assert ct in self.__monitors, \
            "Message from consumer '{}', but we are not monitoring that (list={})".format(
                msg.delivery_info['consumer_tag'], self.__monitors.keys())
        mon = self.__monitors[ct]
        for event_cb in mon['event_cb']:
            event_cb(msg, json.loads(msg.body))

    def stop_greenlet(self):
        self.__running = False

    @property
    def connected(self):
        return self.__connection.connected

    def create_add_tracker(self, exchange, routing_key, event_cb, queue_name=None):
        self.__logs.irl.debug("AMQPServerWrapper: create_add_trcker ex=%s, rk=%s, event_cb=%s", exchange, routing_key, event_cb)
        mname = "ex={} rk={} qn={}".format(exchange, routing_key, queue_name)
        if mname in self.__monitors:
            mon = self.__monitors[mname]
            mon["event_cb"].append(event_cb)
        else:
            if queue_name is None:
                queue_name = ''
                exclusive = True
            else:
                exclusive = False
            ex = Exchange(exchange, 'topic')
            queue = Queue(exchange=ex, routing_key=routing_key, exclusive=exclusive)
            bound_queue = queue.bind(self.__connection)
            self.__consumer.add_queue(bound_queue)
            bound_queue.consume(mname, self.__on_message_cb)
            mon = {
                "event_cb": [event_cb],
                "exchange": ex
            }
            self.__monitors[mname] = mon
        return mon['exchange']

    def inject(self, exchange, routing_key, payload):
        self.__logs.irl.debug("Injecting a test AMQP message: ex=%s, rk=%s, payload=%s", exchange, routing_key, payload)
        if not isinstance(exchange, Exchange):
            exchange = Exchange(exchange, 'topic')
        prod = Producer(self.__connection, exchange=exchange, routing_key=routing_key)
        prod.publish(payload)

    def test_helper_sync_send_msg(self, exchange, ex_rk, send_rk, payload):
        ex = Exchange(exchange, 'topic')
        queue = Queue(exchange=ex, routing_key=ex_rk + '.*', exclusive=True, channel=self.__connection)
        queue.declare()
        prod = Producer(self.__connection, exchange=ex, routing_key=send_rk)
        prod.publish(payload)
        return queue

    def test_helper_sync_recv_msg(self, queue):
        for tick in range(10):
            msg = queue.get()
            if msg is not None:
                break
        return msg


class _AMQPMatcher(StreamMatchBase):
    """
    Implementation of a StreamMatchBase matcher.
    """
    def __init__(self, route_key, description, min=1, max=sys.maxint):
        self.__route_key = route_key
        if route_key is not None:
            escaped_key = re.escape(route_key)
            no_star = escaped_key.replace('*', '[^.]')
            no_pound = no_star.replace('\#', '.*?')

            self.__rk_regex = re.compile('^{}$'.format(no_pound))
            self.__no_pound = no_pound
        else:
            self.__rk_regex = re.compile('.*')
        super(_AMQPMatcher, self).__init__(description, min=min, max=max)

    def _match(self, other_event):
        if self.__route_key is None:
            return bool(other_event)

        assert isinstance(other_event, _AMQPTrackerRecord), \
            'other_event was a {} needs to be a {}'.format(type(other_event), _AMQPTrackerRecord)
        m = self.__rk_regex.match(other_event.msg.delivery_info['routing_key'])
        return m is not None

    def dump(self, ofile=sys.stdout, indent=0):
        super(_AMQPMatcher, self).dump(ofile=ofile, indent=indent)
        ins = ' ' * indent
        print >>ofile, "{0} route_key='{1}'".format(ins, self.__route_key)


class _AMQPProcessor(StreamMonitorBaseClass):
    def __init__(self, logs, tracker, start_at=None, transient=True):
        self._logs = logs
        super(_AMQPProcessor, self).__init__()
        self.handle_begin()
        self.transient = transient
        self.__tracker = tracker
        self.__inbound_queue = gevent.queue.Queue()
        self.__run_till = None
        self.__tail_timeout = None
        self.__in_finish_mode = False
        # THIS is a hack to allow raw access to underlying tracker-records until we get a common
        # validation phase. See get_raw_tracker_events() below for details
        self.__matches_in_order = []
        tracker.add_processor(self, start_at=start_at)
        self.__match_greenlet = gevent.spawn(self.__match_greenlet_run)
        self.__match_greenlet.greenlet_name = 'processor-match-loop-gl'

    def __match_greenlet_run(self):
        self._logs.irl.debug('Starting to watch for events %s', self)
        results = StreamRunResults()

        tail_limit = None
        loop_exit_why = None
        noticed_change_to_finish = False
        while (loop_exit_why is None) and (self.__run_till is not None and self.__run_till > time.time()):
            # we always want to setup tail_limit when we first cross over to finishing
            if not noticed_change_to_finish and self.__in_finish_mode:
                noticed_change_to_finish = True
                self._logs.irl.debug(' Noticed that we shifted to finish-mode')
                if tail_limit is None:
                    tail_limit = time.time() + self.__tail_timeout
                    self._logs.irl.debug('  and set tail-limit from none to %s', tail_limit)

            try:
                # timeout on peek call is needed to allow us to "notice" if our run-till
                # or tail-time has been exceeded.
                tracked = self.__inbound_queue.peek(timeout=0.1)
                self._logs.idl.debug('%s peeked and got %s', self, tracked)
            except gevent.queue.Empty:
                tracked = None

            if tracked is None:
                # no message on queue.
                if tail_limit is not None and time.time() > tail_limit:
                    self._logs.irl.debug(' hit tail limit during idle. Checking if end-check will work')
                    res = self._match_groups.check_ending()
                    if res.is_empty:
                        self._logs.irl.debug('   and we can stop because processor in success state')
                        loop_exit_why = "tail-wait expired while processor in success state"
                    else:
                        # clear the tail-limit till another event hits us
                        self._logs.irl.debug('   and clearing tail-limit since we are not in success state: %s', res)
                        tail_limit = None
                continue

            # So we have an event to look at...
            res = self._match_groups.check_event(tracked)
            consume = False
            if res is not None:
                consume = True
                results.add_result(res)
                self.__matches_in_order.append(tracked)
            elif self.__ignore_misses:
                # note: ignore_miss can only be set as we enter start-finish mode.
                consume = True

            if consume:
                # remove consumed item from queue.
                self.__inbound_queue.get()

                if self.__tail_timeout is not None:
                    # we consumed a message, so bump out tail-limit
                    old_tail_limit = tail_limit
                    tail_limit = time.time() + self.__tail_timeout
                    self._logs.irl.debug('  consumed event. Bumping tail-limit from %s to %s', old_tail_limit, tail_limit)

        if loop_exit_why is None:
            loop_exit_why = "overall timeout occured"
        self._logs.irl.debug('---exiting loop because %s---: %s -> %s', loop_exit_why, self, results)
        res = self._match_groups.check_ending()
        results.add_result(res)
        self._logs.irl.debug('  final results from %s is %s', self, results)
        return results

    def start_finish(self, timeout, tail_timeout=1.0, ignore_misses=True):
        self._logs.irl.debug('start_finish on %s called. timeout=%s, tail-timeout=%s', self, timeout, tail_timeout)
        self.__tail_timeout = tail_timeout
        self.__run_till = time.time() + timeout + tail_timeout
        self.__ignore_misses = ignore_misses
        self.__in_finish_mode = True
        return self.__match_greenlet

    def process_tracked_record(self, tracked_record):
        self._logs.irl.debug('Processing-tracked-record = %s', tracked_record)
        self.__inbound_queue.put(tracked_record)

    def match_any_event(self, description=None, min=1, max=1):
        if description is None:
            description = "match-any(rk={},min={},max={}".format(None, min, max)
        m = _AMQPMatcher(route_key=None, description=description, min=min, max=max)
        self._add_matcher(m)

    def match_on_routekey(self, description=None, routing_key=None, min=1, max=1):
        if routing_key is None:
            routing_key = '#'
        if description is None:
            description = "match-maker-match-maker-name-me-better(rk={},min={},max={}".format(None, min, max)
        m = _AMQPMatcher(route_key=routing_key, description=description, min=min, max=max)
        self._add_matcher(m)

    def get_raw_tracker_events(self):
        """
        total hack method to get raw access to the tracker-events. We WANT a mechanism
        to do a veryify step at end-of-run, but for now this will have to do.
        """
        return self.__matches_in_order


class _AMQPTrackerRecord(object):
    def __init__(self, in_test, prior_test, msg, body):
        self.in_test = str(in_test)
        self.prior_test = str(prior_test)
        self.msg = msg
        self.body = body
        self.timestamp = datetime.now()


class _AMQPQueueTracker(object):
    def __init__(self, tracker_name, logs, amqp_server, exchange_name, routing_key=None):
        self.tracker_name = tracker_name
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self._logs = logs
        # self.handle_begin()
        self.__server = amqp_server
        self.__routing_key = routing_key
        self.__recorded_data = []
        self.__processors = []

        ex = self.__server.create_add_tracker(exchange_name, routing_key, self.__got_amqp_message_cb)
        self.__exchange = ex
        self.__in_test = None
        self.__prior_test = None

    def handle_set_flogging(self, logs):
        self._logs = logs

    def set_test(self, test):
        if self.__in_test is not None:
            self.__prior_test = self.__in_test
            if test is None:
                saved_processors = []
                for processor in self.__processors:
                    if not processor.transient:
                        saved_processors = processor
                    else:
                        self._logs.irl.debug('Removed processor %s', processor)
            self.__processors = saved_processors
        self.__in_test = test

    def __got_amqp_message_cb(self, msg, body):
        self._logs.irl.debug('%s received msg=%s, body=%s', self, msg, body)
        track = _AMQPTrackerRecord(self.__in_test, self.__prior_test, msg, body)
        self.__recorded_data.append(track)
        for processor in self.__processors:
            processor.process_tracked_record(track)

    def add_processor(self, processor, start_at):
        valid_start_ats = [None, 'now']
        assert start_at in valid_start_ats, \
            "start_at of '{}' not one of current valid start_ats {}".format(start_at, valid_start_ats)
        self.__processors.append(processor)
        if start_at is None:
            for tracker_record in self.__recorded_data:
                processor.process_tracked_record(tracker_record)

    def start_finish(self, timeout):
        greenlets = []
        for processor in self.__processors:
            self._logs.irl.debug("%s going to start_finish on %s", self, processor)
            gl = processor.start_finish(timeout)
            greenlets.append(gl)
        self._logs.irl.debug("  list of greenlets to finish %s", greenlets)
        return greenlets

    def test_helper_wait_for_one_message(self, timeout=5):
        sleep_till = time.time() + timeout
        self._logs.irl.debug('waiting for single message, timeout=%s', timeout)
        while len(self.__recorded_data) == 0 and time.time() < sleep_till:
            gevent.sleep(0)
        if len(self.__recorded_data) > 0:
            return self.__recorded_data[0]
        return None

    def __str__(self):
        ns = 'tracker(name={}, ex={}, rk={}'.format(self.tracker_name, self.exchange_name, self.routing_key)
        return ns

    def __repr__(self):
        return str(self)


class AMQPStreamMonitor(StreamMonitorBaseClass):
    """
    Implementation of a StreamMonitorBaseClass that handles working with AMQP.

    Needs to be able to:
    * Create an AMQP-on-demand server if asked
    * Spin up an AMQP receiver greenlet to on-demand
    """
    def handle_set_flogging(self, logs):
        super(AMQPStreamMonitor, self).handle_set_flogging(logs)
        self.__trackers = {}
        self.__call_for_all_trackers('handle_set_flogging)', logs)

    def handle_begin(self):
        """
        Handles plugin 'begin' event. This means spinning up
        a greenlet to monitor the AMQP server.
        """
        super(AMQPStreamMonitor, self).handle_begin()
        sm_amqp_url = getattr(self.__options, 'sm_amqp_url', None)
        sm_amqp_use_user = getattr(self.__options, 'sm_amqp_use_user', None)
        sm_amqp_setup_user = getattr(self.__options, 'sm_amqp_setup_user', None)
        self.__cleanup_user = None
        self.__amqp_on_demand = False
        if sm_amqp_url is None:
            sm_amqp_url = None
        elif sm_amqp_url == 'on-demand':
            self.__amqp_on_demand = RackHDAMQPOnDemand()
            sm_amqp_url = self.__amqp_on_demand.get_url()
        elif sm_amqp_url.startswith('generate'):
            sm_amqp_url, self.__cleanup_user = self.__setup_generated_amqp(sm_amqp_url, sm_amqp_use_user, sm_amqp_setup_user)
        if sm_amqp_url is None:
            self.__amqp_server = None
        else:
            self.__amqp_server = _AMQPServerWrapper(sm_amqp_url, self._logs)

    def __call_for_all_trackers(self, method_name, *args, **kwargs):
        self._logs.irl.debug('relaying %s(%s) to all trackers %s', method_name, args, self.__trackers)
        for tracker in self.__trackers.values():
            method = getattr(tracker, method_name, None)
            if method is not None:
                self._logs.irl.debug_4('   method %s:%s found on monitor %s. calling', method_name, method, tracker)
                method(*args, **kwargs)

    def create_tracker(self, tracker_name, exchange_name, routing_key=None):
        assert tracker_name not in self.__trackers, \
            'you attempted to create a tracker by the name of {}(ex={},rk={}) but it already exists {}'.format(
                tracker_name, exchange_name, routing_key, self.__trackers[tracker_name])
        tracker = _AMQPQueueTracker(tracker_name, self._logs, self.__amqp_server, exchange_name, routing_key=routing_key)
        self.__trackers[tracker_name] = tracker
        self._logs.irl.debug('created tracker {}'.format(tracker))
        return tracker

    def get_tracker_queue_processor(self, tracker, start_at=None):
        assert tracker.tracker_name in self.__trackers, \
            "you tried to use tracker {}, but it isn't in the list of registered trackers {}".format(
                tracker.name, self.__trackers.keys())
        proc = _AMQPProcessor(self._logs, tracker, start_at=start_at)
        return proc

    def handle_start_test(self, test):
        self.__call_for_all_trackers('set_test', test)
        super(AMQPStreamMonitor, self).handle_start_test(test)

    def handle_after_test(self, test):
        self.__call_for_all_trackers('set_test', None)
        super(AMQPStreamMonitor, self).handle_after_test(test)

    def handle_finalize(self):
        """
        Handle end-of-run cleanup
        """
        if self.__cleanup_user is not None:
            clean = SSHHelper('dut', 'amqp-user-delete-ssh-stdouterr: ')
            cmd_text, ecode, output = clean.sendline_and_stat('rabbitmqctl delete_user {}'.format(
                self.__cleanup_user))
            if ecode != 0 and 'no_such_user' not in output:
                self._logs.irl.warning(
                    "remove of amqp-test-user %s command '%s' failed with something other than 'no_such_user': %s",
                    self.__cleanup_user, classmethod, output)
        if self.__amqp_server is not None:
            self.__amqp_server.stop_greenlet()

    def inject(self, exchange, routing_key, payload):
        self.__amqp_server.inject(exchange, routing_key, payload)

    def finish(self, timeout=5):
        greenlets = []
        self._logs.irl.debug("Entering finish for amqp-stream monitor with %d trackers", len(self.__trackers))
        for tracker in self.__trackers.values():
            ttgls = tracker.start_finish(timeout=timeout)
            self._logs.irl.debug("  located %s greenlets (%s) in tracker %s", len(ttgls), tracker, ttgls)
            greenlets.extend(ttgls)
        self._logs.irl.debug("START wait for %d greenlets (%s)", len(greenlets), greenlets)
        gevent.wait(greenlets)
        reses = []
        self._logs.irl.debug("END wait for %d greenlets (%s)", len(greenlets), greenlets)
        for gr in greenlets:
            assert gr.ready(), \
                'all greenlets said they completed, but this one is not {}'.format(gr)
            if not gr.successful():
                raise gr.exception
            assert gr.successful(), \
                'a greenlet {} failed with {}.'.format(gr, gr.exception)
            results = gr.value
            reses.append(results)
            self._logs.irl.debug("  added results %s for greenlet %s", results, gr)
        self._logs.irl.debug("complete set of results for finish: %s", reses)
        return reses

    def __setup_generated_amqp(self, generate_string, use_user, setup_user):
        """
        Handle the case where we are told to generate an AMQP user
        and even set it up on the DUT. There are three paths here:
        * totally auto-setup (use_user and setup_user both None)
        * use_user is non-none, so we use that name instead of creating one (we also don't delete the user when done)
        * setup_user is non-none, so we create one by that name (and don't delete when done)
        """
        assert use_user is None or setup_user is None, \
            "can't both setup user AND use-user in same invocation"
        port = int(generate_string.split(':')[1])
        if use_user is not None:
            auser = use_user
            apw = use_user
            host = SSHHelper.get_parser_options_sm_dut_ssh_host()
            return 'amqp://{}:{}@{}:{}'.format(auser, apw, host, port), None
        elif setup_user is not None:
            auser = setup_user
            apw = setup_user
            ret_user = None
        else:
            uid = str(uuid.uuid4())
            auser = 'tdd_amqp_user_{}'.format(uid)
            apw = uid
            ret_user = auser

        try:
            fixed = SSHHelper('dut', 'amqp-user-setup-ssh-stdouterr: ')
            cmd_text, ecode, output = fixed.sendline_and_stat('rabbitmqctl delete_user {}'.format(auser))
            # the user probably WON'T be there, so don't worry much.
            assert ecode == 0 or 'no_such_user' in output, \
                "{} failed with something other than 'no_such_user':".format(cmd_text, output)
            # now add this user.
            fixed.sendline_and_stat('rabbitmqctl add_user {} {}'.format(auser, apw), must_be_0=True)
            # add administrator tag
            fixed.sendline_and_stat('rabbitmqctl set_user_tags {} administrator'.format(auser), must_be_0=True)
            # now add permissions
            fixed.sendline_and_stat(r'''rabbitmqctl set_permissions {} ".*" ".*" ".*"'''.format(auser), must_be_0=True)
            fixed.logout()
            return 'amqp://{}:{}@{}:{}'.format(auser, apw, fixed.dut_ssh_host, port), ret_user
        except EOF as ex:
            self._logs.irl.warning('unable to connect to instance to setup AMQP user. AMQP monitors disabled: %s', ex)
            self._logs.irl.warning('^^^^ this is -usually- caused by incorrect configuration, such as " \
                "the wrong host or ssh port for the given installation')
        except Exception as ex:
            self._logs.irl.debug('unable to set up amqp user. AMQP monitors disabled: %s', ex)
            self._logs.irl.debug('^^^^ if this is a deploy test, this is probably ok. If it is a real test, this is a problem.')
        return None, None

    @property
    def has_amqp_server(self):
        """
        method to indicate if an AMQP server was defined or not.
        This allows callers to Skip() tests if not.
        """
        return self.__amqp_server is not None

    def test_helper_is_amqp_running(self):
        return self.__amqp_server.connected

    def test_helper_sync_send_msg(self, exchange, ex_rk, send_rk, payload):
        return self.__amqp_server.test_helper_sync_send_msg(
            exchange, ex_rk, send_rk, payload)

    def test_helper_sync_recv_msg(self, queue):
        return self.__amqp_server.test_helper_sync_recv_msg(queue)

    @classmethod
    def enabled_for_nose(true):
        return True

    def set_options(self, options):
        self.__options = options

    @classmethod
    def add_nose_parser_opts(self, parser):
        amqp_group = optparse.OptionGroup(parser, 'AMQP options')
        parser.add_option_group(amqp_group)
        amqp_group.add_option(
            '--sm-amqp-url', dest='sm_amqp_url', default=None,
            help="set the AMQP url to use. If not set, a docker based server will be setup and used")
        amqp_group.add_option(
            '--sm-amqp-setup-user', dest='sm_amqp_setup_user', default=None,
            help="assure this user exists in the instance. Disables the auto-create user")
        amqp_group.add_option(
            '--sm-amqp-use-user', dest='sm_amqp_use_user', default=None,
            help="use this user instead of auto-creating one. Must already exist in instance")