import asyncio
import time
from aiokafka.consumer import AIOKafkaConsumer
from aiokafka.fetcher import RecordTooLargeError
from aiokafka.producer import AIOKafkaProducer

from kafka.common import (
    TopicPartition, OffsetAndMetadata, IllegalStateError,
    UnknownTopicOrPartitionError)
from ._testutil import (
    KafkaIntegrationTestCase, run_until_complete, random_string)


class TestConsumerIntegration(KafkaIntegrationTestCase):
    @asyncio.coroutine
    def consumer_factory(self, **kwargs):
        enable_auto_commit = kwargs.pop('enable_auto_commit', True)
        auto_offset_reset = kwargs.pop('auto_offset_reset', 'earliest')
        group = kwargs.pop('group', 'group-%s' % self.id())
        consumer = AIOKafkaConsumer(
            self.topic, loop=self.loop, group_id=group,
            bootstrap_servers=self.hosts,
            enable_auto_commit=enable_auto_commit,
            auto_offset_reset=auto_offset_reset,
            **kwargs)
        yield from consumer.start()
        if group is not None:
            yield from consumer.seek_to_committed()
        return consumer

    @run_until_complete
    def test_simple_consumer(self):
        with self.assertRaises(ValueError):
            # check unsupported version
            consumer = yield from self.consumer_factory(api_version="0.8")

        yield from self.send_messages(0, list(range(0, 100)))
        yield from self.send_messages(1, list(range(100, 200)))
        # Start a consumer_factory
        consumer = yield from self.consumer_factory()

        p0 = TopicPartition(self.topic, 0)
        p1 = TopicPartition(self.topic, 1)
        assignment = consumer.assignment()
        self.assertEqual(sorted(list(assignment)), [p0, p1])

        topics = yield from consumer.topics()
        self.assertTrue(self.topic in topics)

        parts = consumer.partitions_for_topic(self.topic)
        self.assertEqual(sorted(list(parts)), [0, 1])

        offset = yield from consumer.committed(
            TopicPartition("uknown-topic", 2))
        self.assertEqual(offset, None)

        offset = yield from consumer.committed(p0)
        if offset is None:
            offset = 0

        messages = []
        for i in range(200):
            message = yield from consumer.getone()
            messages.append(message)
        self.assert_message_count(messages, 200)

        h = consumer.highwater(p0)
        self.assertEqual(h, 100)

        consumer.seek(p0, offset + 90)
        for i in range(10):
            m = yield from consumer.getone()
            self.assertEqual(m.value, str(i + 90).encode())
        yield from consumer.stop()

        # will ignore, no exception expected
        yield from consumer.stop()

    @run_until_complete
    def test_get_by_partition(self):
        yield from self.send_messages(0, list(range(0, 100)))
        yield from self.send_messages(1, list(range(100, 200)))
        consumer = yield from self.consumer_factory()

        p0 = TopicPartition(self.topic, 0)
        p1 = TopicPartition(self.topic, 1)
        messages = []

        @asyncio.coroutine
        def task(tp, messages):
            for i in range(100):
                m = yield from consumer.getone(tp)
                self.assertEqual(m.partition, tp.partition)
                messages.append(m)

        task1 = asyncio.async(task(p0, messages), loop=self.loop)
        task2 = asyncio.async(task(p1, messages), loop=self.loop)
        yield from asyncio.wait([task1, task2], loop=self.loop)
        self.assert_message_count(messages, 200)
        yield from consumer.stop()

    @run_until_complete
    def test_none_group(self):
        yield from self.send_messages(0, list(range(0, 100)))
        yield from self.send_messages(1, list(range(100, 200)))
        # Start a consumer_factory
        consumer1 = yield from self.consumer_factory(
            group=None, enable_auto_commit=False)
        consumer2 = yield from self.consumer_factory(group=None)

        messages = []
        for i in range(200):
            message = yield from consumer1.getone()
            messages.append(message)
        self.assert_message_count(messages, 200)
        with self.assertRaises(AssertionError):
            # commit does not supported for None group
            yield from consumer1.commit()

        messages = []
        for i in range(200):
            message = yield from consumer2.getone()
            messages.append(message)
        self.assert_message_count(messages, 200)
        yield from consumer1.stop()
        yield from consumer2.stop()

    @run_until_complete
    def test_consumer_poll(self):
        yield from self.send_messages(0, list(range(0, 100)))
        yield from self.send_messages(1, list(range(100, 200)))
        # Start a consumer_factory
        consumer = yield from self.consumer_factory()

        messages = []
        while True:
            resp = yield from consumer.getmany(timeout_ms=1000)
            for partition, msg_list in resp.items():
                messages += msg_list
            if len(messages) == 200:
                break
        self.assert_message_count(messages, 200)

        p0 = TopicPartition(self.topic, 0)
        p1 = TopicPartition(self.topic, 1)
        yield from self.send_messages(0, list(range(0, 100)))
        yield from self.send_messages(1, list(range(100, 200)))

        messages = []
        while True:
            resp = yield from consumer.getmany(p0, timeout_ms=1000)
            for partition, msg_list in resp.items():
                messages += msg_list
            if len(messages) == 100:
                break
        self.assert_message_count(messages, 100)

        while True:
            resp = yield from consumer.getmany(p1)
            yield from asyncio.sleep(0.1, loop=self.loop)
            for partition, msg_list in resp.items():
                messages += msg_list
            if len(messages) == 200:
                break
        self.assert_message_count(messages, 200)

        yield from consumer.stop()

    @run_until_complete
    def test_large_messages(self):
        # Produce 10 "normal" size messages
        r_msgs = [str(x) for x in range(10)]
        small_messages = yield from self.send_messages(0, r_msgs)

        # Produce 10 messages that are large (bigger than default fetch size)
        l_msgs = [random_string(5000) for _ in range(10)]
        large_messages = yield from self.send_messages(0, l_msgs)

        # Consumer should still get all of them
        consumer = yield from self.consumer_factory()
        expected_messages = set(small_messages + large_messages)
        actual_messages = []
        for i in range(20):
            m = yield from consumer.getone()
            actual_messages.append(m)
        actual_messages = {m.value for m in actual_messages}
        self.assertEqual(expected_messages, set(actual_messages))
        yield from consumer.stop()

    @run_until_complete
    def test_too_large_messages(self):
        l_msgs = [random_string(10), random_string(50000)]
        large_messages = yield from self.send_messages(0, l_msgs)
        r_msgs = [random_string(50)]
        small_messages = yield from self.send_messages(0, r_msgs)

        consumer = yield from self.consumer_factory(
            max_partition_fetch_bytes=4000)
        m = yield from consumer.getone()
        self.assertEqual(m.value, large_messages[0])

        with self.assertRaises(RecordTooLargeError):
            yield from consumer.getone()

        m = yield from consumer.getone()
        self.assertEqual(m.value, small_messages[0])
        yield from consumer.stop()

    @run_until_complete
    def test_offset_behavior__resuming_behavior(self):
        msgs1 = yield from self.send_messages(0, range(0, 100))
        msgs2 = yield from self.send_messages(1, range(100, 200))

        available_msgs = msgs1 + msgs2
        # Start a consumer_factory
        consumer1 = yield from self.consumer_factory()
        consumer2 = yield from self.consumer_factory()
        result = []
        for i in range(10):
            msg = yield from consumer1.getone()
            result.append(msg.value)
        yield from consumer1.stop()

        # consumer2 should take both partitions after rebalance
        while True:
            msg = yield from consumer2.getone()
            result.append(msg.value)
            if len(result) == len(available_msgs):
                break

        yield from consumer2.stop()
        if consumer1._client.api_version < (0, 9):
            # coordinator rebalance feature works with >=Kafka-0.9 only
            return
        self.assertEqual(set(available_msgs), set(result))
        yield from consumer1.stop()
        yield from consumer2.stop()

    @run_until_complete
    def test_subscribe_manual(self):
        msgs1 = yield from self.send_messages(0, range(0, 10))
        msgs2 = yield from self.send_messages(1, range(10, 20))
        available_msgs = msgs1 + msgs2

        consumer = yield from self.consumer_factory()
        pos = yield from consumer.position(TopicPartition(self.topic, 0))
        with self.assertRaises(IllegalStateError):
            consumer.assign([TopicPartition(self.topic, 0)])
        consumer.unsubscribe()
        consumer.assign([TopicPartition(self.topic, 0)])
        result = []
        for i in range(10):
            msg = yield from consumer.getone()
            result.append(msg.value)
        self.assertEqual(set(result), set(msgs1))
        yield from consumer.commit()
        pos = yield from consumer.position(TopicPartition(self.topic, 0))
        self.assertTrue(pos > 0)

        consumer.unsubscribe()
        consumer.assign([TopicPartition(self.topic, 1)])
        for i in range(10):
            msg = yield from consumer.getone()
            result.append(msg.value)
        yield from consumer.stop()
        self.assertEqual(set(available_msgs), set(result))

    @run_until_complete
    def test_manual_subscribe_pattern(self):
        msgs1 = yield from self.send_messages(0, range(0, 10))
        msgs2 = yield from self.send_messages(1, range(10, 20))
        available_msgs = msgs1 + msgs2

        consumer = AIOKafkaConsumer(
            loop=self.loop, group_id='test-group',
            bootstrap_servers=self.hosts, auto_offset_reset='earliest',
            enable_auto_commit=False)
        consumer.subscribe(pattern="topic-test_manual_subs*")
        yield from consumer.start()
        yield from consumer.seek_to_committed()
        result = []
        for i in range(20):
            msg = yield from consumer.getone()
            result.append(msg.value)
        self.assertEqual(set(available_msgs), set(result))

        yield from consumer.commit(
            {TopicPartition(self.topic, 0): OffsetAndMetadata(9, '')})
        yield from consumer.seek_to_committed(TopicPartition(self.topic, 0))
        msg = yield from consumer.getone(TopicPartition(self.topic, 0))
        self.assertEqual(msg.value, b'9')
        yield from consumer.commit(
            {TopicPartition(self.topic, 0): OffsetAndMetadata(10, '')})
        yield from consumer.stop()

        # subscribe by topic
        consumer = AIOKafkaConsumer(
            loop=self.loop, group_id='test-group',
            bootstrap_servers=self.hosts, auto_offset_reset='earliest',
            enable_auto_commit=False)
        consumer.subscribe(topics=(self.topic,))
        yield from consumer.start()
        yield from consumer.seek_to_committed()
        result = []
        for i in range(10):
            msg = yield from consumer.getone()
            result.append(msg.value)
        self.assertEqual(set(msgs2), set(result))
        self.assertEqual(consumer.subscription(), set([self.topic]))
        yield from consumer.stop()

    @run_until_complete
    def test_compress_decompress(self):
        producer = AIOKafkaProducer(
            loop=self.loop, bootstrap_servers=self.hosts,
            compression_type="gzip")
        yield from producer.start()
        yield from self.wait_topic(producer.client, self.topic)
        msg1 = b'some-message' * 10
        msg2 = b'other-message' * 30
        yield from producer.send(self.topic, msg1, partition=1)
        yield from producer.send(self.topic, msg2, partition=1)
        yield from producer.stop()

        consumer = yield from self.consumer_factory()
        rmsg1 = yield from consumer.getone()
        self.assertEqual(rmsg1.value, msg1)
        rmsg2 = yield from consumer.getone()
        self.assertEqual(rmsg2.value, msg2)
        yield from consumer.stop()

    @run_until_complete
    def test_compress_decompress_lz4(self):
        producer = AIOKafkaProducer(
            loop=self.loop, bootstrap_servers=self.hosts,
            compression_type="lz4")
        yield from producer.start()
        yield from self.wait_topic(producer.client, self.topic)
        msg1 = b'some-message' * 10
        msg2 = b'other-message' * 30
        yield from producer.send(self.topic, msg1, partition=1)
        yield from producer.send(self.topic, msg2, partition=1)
        yield from producer.stop()

        consumer = yield from self.consumer_factory()
        rmsg1 = yield from consumer.getone()
        self.assertEqual(rmsg1.value, msg1)
        rmsg2 = yield from consumer.getone()
        self.assertEqual(rmsg2.value, msg2)
        yield from consumer.stop()

    @run_until_complete
    def test_consumer_seek_backward(self):
        # Send 2 messages
        yield from self.send_messages(0, [1, 2])

        # Read first. 2 are delivered at a time, so 1 will remain
        consumer = yield from self.consumer_factory()
        rmsg1 = yield from consumer.getone()
        self.assertEqual(rmsg1.value, b'1')

        # Seek should invalidate the remaining message
        tp = TopicPartition(self.topic, rmsg1.partition)
        consumer.seek(tp, rmsg1.offset)
        rmsg2 = yield from consumer.getone()
        self.assertEqual(rmsg2.value, b'1')
        rmsg2 = yield from consumer.getone()
        self.assertEqual(rmsg2.value, b'2')
        yield from consumer.stop()

    @run_until_complete
    def test_consumer_seek_forward(self):
        # Send 3 messages
        yield from self.send_messages(0, [1, 2, 3])

        # Read first. 3 are delivered at a time, so 2 will remain
        consumer = yield from self.consumer_factory()
        rmsg1 = yield from consumer.getone()
        self.assertEqual(rmsg1.value, b'1')

        # Seek should invalidate the remaining message
        tp = TopicPartition(self.topic, rmsg1.partition)
        consumer.seek(tp, rmsg1.offset + 2)
        rmsg2 = yield from consumer.getone()
        self.assertEqual(rmsg2.value, b'3')
        res = yield from consumer.getmany(timeout_ms=0)
        self.assertEqual(res, {tp: []})
        yield from consumer.stop()

    @run_until_complete
    def test_manual_subscribe_nogroup(self):
        msgs1 = yield from self.send_messages(0, range(0, 10))
        msgs2 = yield from self.send_messages(1, range(10, 20))
        available_msgs = msgs1 + msgs2

        consumer = AIOKafkaConsumer(
            loop=self.loop, group_id=None,
            bootstrap_servers=self.hosts, auto_offset_reset='earliest',
            enable_auto_commit=False)
        consumer.subscribe(topics=(self.topic,))
        yield from consumer.start()
        result = []
        for i in range(20):
            msg = yield from consumer.getone()
            result.append(msg.value)
        self.assertEqual(set(available_msgs), set(result))
        yield from consumer.stop()

    @run_until_complete
    def test_unknown_topic_or_partition(self):
        consumer = AIOKafkaConsumer(
            loop=self.loop, group_id=None,
            bootstrap_servers=self.hosts, auto_offset_reset='earliest',
            enable_auto_commit=False)
        consumer.subscribe(topics=('some_topic_unknown',))
        with self.assertRaises(UnknownTopicOrPartitionError):
            yield from consumer.start()

        with self.assertRaises(UnknownTopicOrPartitionError):
            yield from consumer.assign([TopicPartition(self.topic, 2222)])
        yield from consumer.stop()

    @run_until_complete
    def test_check_extended_message_record(self):
        s_time_ms = time.time() * 1000

        producer = AIOKafkaProducer(
            loop=self.loop, bootstrap_servers=self.hosts)
        yield from producer.start()
        yield from self.wait_topic(producer.client, self.topic)
        msg1 = b'some-message#1'
        yield from producer.send(self.topic, msg1, partition=1)
        yield from producer.stop()

        consumer = yield from self.consumer_factory()
        rmsg1 = yield from consumer.getone()
        self.assertEqual(rmsg1.value, msg1)
        self.assertEqual(rmsg1.serialized_key_size, -1)
        self.assertEqual(rmsg1.serialized_value_size, 14)
        if consumer._client.api_version >= (0, 10):
            self.assertNotEqual(rmsg1.timestamp, None)
            self.assertTrue(rmsg1.timestamp >= s_time_ms)
            self.assertEqual(rmsg1.timestamp_type, 0)
        else:
            self.assertEqual(rmsg1.timestamp, None)
            self.assertEqual(rmsg1.timestamp_type, None)
        yield from consumer.stop()

    @run_until_complete
    def test_equal_consumption(self):
        # A strange use case of kafka-python, that can be reproduced in
        # aiokafka https://github.com/dpkp/kafka-python/issues/675
        yield from self.send_messages(0, list(range(200)))
        yield from self.send_messages(1, list(range(200)))

        partition_consumption = [0, 0]
        for x in range(10):
            consumer = yield from self.consumer_factory(
                max_partition_fetch_bytes=10000)
            for x in range(10):
                msg = yield from consumer.getone()
                partition_consumption[msg.partition] += 1
            yield from consumer.stop()

        diff = abs(partition_consumption[0] - partition_consumption[1])
        # We are good as long as it's not 100%, as we do rely on randomness of
        # a shuffle in code. Ideally it should be 50/50 (0 diff) thou
        self.assertLess(diff / sum(partition_consumption), 1.0)

    @run_until_complete
    def test_max_poll_records(self):
        # A strange use case of kafka-python, that can be reproduced in
        # aiokafka https://github.com/dpkp/kafka-python/issues/675
        yield from self.send_messages(0, list(range(100)))

        consumer = yield from self.consumer_factory(
            max_poll_records=48)
        data = yield from consumer.getmany(timeout_ms=1000)
        count = sum(map(len, data.values()))
        self.assertEqual(count, 48)
        data = yield from consumer.getmany(timeout_ms=1000, max_records=42)
        count = sum(map(len, data.values()))
        self.assertEqual(count, 42)
        data = yield from consumer.getmany(timeout_ms=1000, max_records=None)
        count = sum(map(len, data.values()))
        self.assertEqual(count, 10)

        with self.assertRaises(ValueError):
            data = yield from consumer.getmany(max_records=0)
        yield from consumer.stop()

        with self.assertRaises(ValueError):
            consumer = yield from self.consumer_factory(
                max_poll_records=0)

    @run_until_complete
    def test_ssl_consume(self):
        # Produce by PLAINTEXT, Consume by SSL
        # Send 3 messages
        yield from self.send_messages(0, [1, 2, 3])

        context = self.create_ssl_context()
        group = "group-{}".format(self.id())
        consumer = AIOKafkaConsumer(
            self.topic, loop=self.loop, group_id=group,
            bootstrap_servers=[
                "{}:{}".format(self.kafka_host, self.kafka_ssl_port)],
            enable_auto_commit=True,
            auto_offset_reset="earliest",
            security_protocol="SSL", ssl_context=context)
        yield from consumer.start()
        results = yield from consumer.getmany(timeout_ms=1000)
        [msgs] = results.values()  # only 1 partition anyway
        msgs = [msg.value for msg in msgs]
        self.assertEqual(msgs, [b"1", b"2", b"3"])
        yield from consumer.stop()

    def test_consumer_arguments(self):
        with self.assertRaisesRegexp(
                ValueError, "`security_protocol` should be SSL or PLAINTEXT"):
            AIOKafkaConsumer(
                self.topic, loop=self.loop,
                bootstrap_servers=self.hosts,
                security_protocol="SOME")
        with self.assertRaisesRegexp(
                ValueError, "`ssl_context` is mandatory if "
                            "security_protocol=='SSL'"):
            AIOKafkaConsumer(
                self.topic, loop=self.loop,
                bootstrap_servers=self.hosts,
                security_protocol="SSL", ssl_context=None)

    @run_until_complete
    def test_consumer_group_without_subscription(self):
        consumer = AIOKafkaConsumer(
            loop=self.loop,
            group_id='group-{}'.format(self.id()),
            bootstrap_servers=self.hosts,
            enable_auto_commit=False,
            auto_offset_reset='earliest',
            heartbeat_interval_ms=100)
        yield from consumer.start()
        yield from asyncio.sleep(0.2, loop=self.loop)
        yield from consumer.stop()
