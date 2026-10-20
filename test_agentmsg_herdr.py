#!/usr/bin/env python3
"""Herdr transport 测试：仅 mock CLI，不接触用户 Herdr Session。"""
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import agentmsg as am
import agentmsg_herdr as ah


class MockHerdr:
    def __init__(self, status="idle", fail_prompt=False):
        self.status = status
        self.fail_prompt = fail_prompt
        self.calls = []
    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[1:3] == ["agent", "get"]:
            import json
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"result": {"agent": {"agent_status": self.status}}}), "")
        if cmd[1:3] == ["agent", "prompt"]:
            if self.fail_prompt:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)


class TestHerdrTransport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "mail.db")
    def tearDown(self):
        # ponytail: 双保险——close_open_conns 应已收干净；万一有漏网连接删不掉就留给系统临时目录
        try:
            self.temp.cleanup()
        except PermissionError:
            pass
    def call(self, *args):
        parsed = am.build().parse_args(["--db", self.db, *[str(i) for i in args]])
        try:
            with redirect_stdout(io.StringIO()):
                parsed.fn(parsed)
        finally:
            am.close_open_conns()
    def test_status_parser(self):
        self.assertEqual(ah.agent_status('{"result":{"agent":{"agent_status":"idle"}}}'), "idle")
        self.assertIsNone(ah.agent_status('{"result":{}}'))
        self.assertIsNone(ah.agent_status('malformed JSON'))
        self.assertIsNone(ah.agent_status('{"result":{"agent":{"status":"success"}}}'))
        self.assertEqual(ah.agent_status('{"result":{"agent":{"status":"done"}}}'), "done")
        self.assertEqual(ah.agent_status('{"result":{"agent_status":"idle"}}'), "idle")
    def test_register_and_switch(self):
        self.call("register", "tester", "--kind", "omp", "--herdr-target", "tester")
        c = am.connect(am.build().parse_args(["--db", self.db, "agents"]), register=False)
        r = c.execute("select transport,herdr_target,tern_block_id from agents where name='tester'").fetchone()
        self.assertEqual((r["transport"], r["herdr_target"], r["tern_block_id"]), ("herdr", "tester", None))
        c.close()
        self.call("register", "tester", "--block", 111)
        c = am.connect(am.build().parse_args(["--db", self.db, "agents"]), register=False)
        r = c.execute("select transport,herdr_target,tern_block_id from agents where name='tester'").fetchone()
        self.assertEqual((r["transport"], r["herdr_target"], r["tern_block_id"]), ("tern", None, 111))
        c.close()
    def test_idle_delivers_notification_not_full_body(self):
        self.call("register", "tester", "--kind", "omp", "--herdr-target", "tester")
        self.call("send", "dev", "tester", "敏感内容" * 100)
        cli = MockHerdr()
        c = am.connect(am.build().parse_args(["--db", self.db, "agents"]), register=False)
        with mock.patch.object(ah.subprocess, "run", cli), mock.patch.dict(os.environ, {"HERDR_ENV":"1"}):
            self.assertEqual(am.deliver_one(c, "tester"), "delivered")
        calls = [x for x in cli.calls if x[1:3] == ["agent", "prompt"]]
        self.assertEqual(len(calls), 1)
        self.assertIn("python agentmsg.py read 1", calls[0][4])
        self.assertNotIn("敏感内容", calls[0][4])
        self.assertEqual(c.execute("select status from messages").fetchone()["status"], "delivered")
        c.close()
    def test_busy_blocked_unknown_skip(self):
        for status in ("working","blocked","unknown"):
            with self.subTest(status=status):
                self.call("register", "tester", "--herdr-target", "tester")
                self.call("send", "dev", "tester", f"hello {status}")
                cli=MockHerdr(status)
                c = am.connect(am.build().parse_args(["--db", self.db, "agents"]), register=False)
                with mock.patch.object(ah.subprocess,"run",cli), mock.patch.dict(os.environ,{"HERDR_ENV":"1"}):
                    self.assertEqual(am.deliver_one(c,"tester"),"skipped")
                self.assertFalse(any(x[1:3] == ["agent","prompt"] for x in cli.calls))
                c.close()
    def test_failure_reverts_claim(self):
        self.call("register","tester","--herdr-target","tester")
        self.call("send","dev","tester","hello")
        cli=MockHerdr(fail_prompt=True)
        c=am.connect(am.build().parse_args(["--db",self.db,"agents"]), register=False)
        with mock.patch.object(ah.subprocess,"run",cli), mock.patch.dict(os.environ,{"HERDR_ENV":"1"}):
            self.assertEqual(am.deliver_one(c,"tester"),"failed")
        self.assertEqual(c.execute("select status from messages").fetchone()["status"],"pending")
        c.close()
    def test_no_unmanaged_session_control(self):
        cli=MockHerdr()
        with mock.patch.object(ah.subprocess,"run",cli), mock.patch.dict(os.environ,{"HERDR_ENV":""}):
            with self.assertRaises(RuntimeError):
                ah.submit_notification("tester","hi")
        self.assertEqual(cli.calls, [])


if __name__ == "__main__":
    unittest.main()
