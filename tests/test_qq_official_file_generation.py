import asyncio
from dataclasses import replace
import unittest

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.file_generation import GenerationBindingAdapter
from tests.qq_official_file_fixture import FileFixture


class GenerationBindingTests(FileFixture):
    def setUp(self):
        super().setUp()
        self.calls=DatabaseCalls()
        self.addCleanup(lambda:asyncio.run(self.calls.close()))
        self.binding=GenerationBindingAdapter(self.source,self.files,self.calls,account_limit=3,actor_limit=2)

    def test_capacity_aggregate_includes_verifier_owner(self):
        async def run():
            first=await self.binding.reserve_capacity(self.binding.owner)
            await self.binding.reserve_capacity(self.binding.owner+':verify')
            with self.assertRaisesRegex(ValueError,'exhausted'):
                await self.binding.reserve_capacity(self.binding.owner)
            await self.binding.release_capacity(first)
            await self.binding.reserve_capacity(self.binding.owner)
        asyncio.run(run())
        self.assertEqual(self.query("SELECT count(*) AS n FROM qq_official_generation_capacity WHERE state='reserved'")[0]['n'],2)

    def test_capacity_concurrent_reservations_do_not_exceed_limit(self):
        async def run():
            results=await asyncio.gather(*(self.binding.reserve_capacity(self.binding.owner) for _ in range(8)),return_exceptions=True)
            self.assertEqual(sum(not isinstance(x,BaseException) for x in results),2)
        asyncio.run(run())
        self.assertEqual(len(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'")),2)

    def test_capacity_forged_release_or_owner_cannot_refund(self):
        async def run():
            item=await self.binding.reserve_capacity(self.binding.owner)
            with self.assertRaises(LeaseLost):
                await self.binding.release_capacity(replace(item))
            with self.assertRaises(LeaseLost):
                await self.binding.reserve_capacity(self.binding.owner+':FOREIGN')
        asyncio.run(run())
        self.assertEqual(len(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'")),1)

    def test_capacity_expiry_does_not_assume_unknown_create_absent(self):
        async def run():
            item=await self.binding.reserve_capacity(self.binding.owner)
            self.now=self.deadline+1
            with self.assertRaises(LeaseLost):
                await self.binding.release_capacity(item)
        asyncio.run(run())
        self.assertEqual(len(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'")),1)


if __name__ == "__main__":
    unittest.main()
