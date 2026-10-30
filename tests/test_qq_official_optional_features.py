"""An optional factory failure still permits a real gateway command reply."""
import unittest
from tests import test_qq_official_file_composition as fixture
from tests.test_qq_official_runtime import loaded_config


class OptionalFeatureTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.FileCompositionTests.setUp
    asyncSetUp = fixture.FileCompositionTests.asyncSetUp
    runtime = fixture.FileCompositionTests.runtime
    query = fixture.FileCompositionTests.query
    wait_db = fixture.FileCompositionTests.wait_db
    application = fixture.FileCompositionTests.application
    emit = fixture.FileCompositionTests.emit
    complete = fixture.FileCompositionTests.complete

    async def test_unavailable_files_media_and_background_do_not_pause_help(self):
        def unavailable(*args):
            raise ValueError('TEST optional feature unavailable')
        app=self.application()
        runtime=self.runtime(loaded=loaded_config(self.fake.app_id,base=self.fake.base_url),application=app,
            ledger=app.message_ledger,journal=app.turn_journal,file_services_factory=unavailable,
            media_factory=unavailable,background_factory=unavailable,approval_factory=unavailable)
        try:
            self.assertTrue(await runtime.start())
            await self.fake.wait_ready()
            await self.emit(self.fake,native='TEST_OPTIONAL_HELP',content='/help')
            await self.complete('TEST_OPTIONAL_HELP')
            self.assertIn('/help',str(self.fake.received[-1]['payload']))
            self.assertIsNotNone(runtime.sender)
        finally:
            await runtime.stop()
