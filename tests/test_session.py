import asyncio
import unittest
from unittest.mock import AsyncMock

from support import ModalTestCase, fake_sandbox

from agentix import sandbox
from agentix.errors import InvalidRequest
from agentix.session import Session


class SessionTests(ModalTestCase):
    def run_with(self, scenario, **options):
        async def run():
            session = Session(**options)
            try:
                return await scenario(session)
            finally:
                await session.close()

        return asyncio.run(run())

    def test_later_operations_reuse_the_handle_and_the_app(self):
        async def scenario(session):
            await sandbox.execute(session, "sb-test", ["true"])
            await sandbox.execute(session, "sb-test", ["true"])
            await sandbox.list_ids(session)
            await sandbox.list_ids(session)

        self.run_with(scenario)
        self.lookup.aio.assert_called_once_with(sandbox_id="sb-test")
        self.app_lookup.aio.assert_called_once()

    def test_operations_that_start_together_share_one_lookup(self):
        async def slow_lookup(sandbox_id):
            await asyncio.sleep(0.01)
            return self.sandbox

        self.lookup.aio = AsyncMock(side_effect=slow_lookup)

        async def scenario(session):
            await asyncio.gather(
                *(sandbox.execute(session, "sb-test", ["true"]) for _ in range(5))
            )

        self.run_with(scenario)
        self.lookup.aio.assert_called_once()
        self.assertEqual(self.sandbox.exec.aio.call_count, 5)

    def test_created_sandbox_needs_no_lookup(self):
        async def scenario(session):
            sandbox_id = await sandbox.create(session, secret_name="")
            await sandbox.execute(session, sandbox_id, ["true"])

        self.run_with(scenario)
        self.lookup.aio.assert_not_called()

    def test_failed_use_drops_the_handle(self):
        self.sandbox.exec.aio.side_effect = [ConnectionError("gone"), None]

        async def scenario(session):
            with self.assertRaises(ConnectionError):
                await sandbox.execute(session, "sb-test", ["true"])
            self.sandbox.exec.aio.side_effect = None
            await sandbox.execute(session, "sb-test", ["true"])

        self.run_with(scenario)
        self.assertEqual(self.lookup.aio.call_count, 2)
        self.sandbox.detach.aio.assert_called()

    def test_failed_lookup_is_tried_again(self):
        self.lookup.aio.side_effect = [ConnectionError("no network"), self.sandbox]

        async def scenario(session):
            with self.assertRaises(ConnectionError):
                await sandbox.execute(session, "sb-test", ["true"])
            return await sandbox.execute(session, "sb-test", ["true"])

        self.assertEqual(self.run_with(scenario)["exit_code"], 0)

    def test_rejected_request_keeps_the_handle(self):
        async def scenario(session):
            async with session.sandbox("sb-test"):
                pass
            with self.assertRaises(InvalidRequest):
                async with session.sandbox("sb-test"):
                    raise InvalidRequest("not this way")
            async with session.sandbox("sb-test"):
                pass

        self.run_with(scenario)
        self.lookup.aio.assert_called_once()

    def test_oldest_handle_is_released_at_the_limit(self):
        handles = {name: fake_sandbox(name) for name in ("sb-1", "sb-2", "sb-3")}
        self.lookup.aio = AsyncMock(side_effect=lambda sandbox_id: handles[sandbox_id])

        async def scenario(session):
            for name in ("sb-1", "sb-2", "sb-1", "sb-3"):
                async with session.sandbox(name):
                    pass

        self.run_with(scenario, max_sandboxes=2)
        handles["sb-2"].detach.aio.assert_called_once()
        self.assertEqual(self.lookup.aio.call_count, 3)

    def test_terminated_sandbox_is_forgotten(self):
        async def scenario(session):
            await sandbox.terminate(session, "sb-test")
            await sandbox.execute(session, "sb-test", ["true"])

        self.run_with(scenario)
        self.assertEqual(self.lookup.aio.call_count, 2)

    def test_new_sandbox_is_used_without_a_lookup(self):
        async def scenario(session):
            sandbox_id = await sandbox.create(session, secret_name="")
            await sandbox.execute(session, sandbox_id, ["true"])

        self.run_with(scenario)
        self.lookup.aio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
