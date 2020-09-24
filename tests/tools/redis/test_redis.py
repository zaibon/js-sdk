from unittest import TestCase
import random
from jumpscale.loader import j


class TestRedis(TestCase):
    def _get_instance(self):
        self.instance_name = j.data.random_names.random_name()
        redis_instance = j.tools.redis.new(self.instance_name)
        return redis_instance

    def _get_port(self, proc):
        conn = proc.connections()[0]
        return conn.laddr[1]

    def test001_redis_start_stop(self):
        port = random.randint(20000, 25000)
        redis_instance = self._get_instance()
        redis_instance.port = port
        redis_instance.save()
        redis_instance.start()
        self.assertTrue(
            j.sals.nettools.wait_connection_test(
                redis_instance.host, redis_instance.port, 2
            )
        )

        self.assertTrue(redis_instance.cmd.is_running())
        self.assertTrue(redis_instance.cmd.process.pid)
        self.assertTrue(redis_instance.cmd.process)

        proc_port = self._get_port(redis_instance.cmd.process)
        self.assertTrue(redis_instance.cmd.is_running())
        self.assertEqual(proc_port, port)

        redis_instance.stop()
        self.assertFalse(
            j.sals.nettools.wait_connection_test(
                redis_instance.host, redis_instance.port, 2
            )
        )
        self.assertFalse(redis_instance.cmd.is_running())
        self.assertFalse(redis_instance.cmd.process)

    def test001_redis_restart(self):
        port = random.randint(20000, 25000)
        redis_instance = self._get_instance()
        redis_instance.port = port
        redis_instance.save()
        redis_instance.start()
        self.assertTrue(
            j.sals.nettools.wait_connection_test(
                redis_instance.host, redis_instance.port, 2
            )
        )

        self.assertTrue(redis_instance.cmd.is_running())
        self.assertTrue(redis_instance.cmd.process.pid)
        self.assertTrue(redis_instance.cmd.process)

        pid = redis_instance.cmd.process.pid

        proc_port = self._get_port(redis_instance.cmd.process)
        self.assertTrue(redis_instance.cmd.is_running())
        self.assertEqual(proc_port, port)

        redis_instance.restart()

        self.assertTrue(
            j.sals.nettools.wait_connection_test(
                redis_instance.host, redis_instance.port, 2
            )
        )
        self.assertTrue(redis_instance.cmd.is_running())
        self.assertTrue(redis_instance.cmd.process)
        self.assertNotEqual(redis_instance.cmd.process.pid, pid)

    def tearDown(self):
        redis_instance = j.tools.redis.find(self.instance_name)
        redis_instance.stop()
        j.tools.redis.delete(self.instance_name)
