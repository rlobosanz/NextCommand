import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from nextcommand.sim.streamio import FakeStreamIO  # noqa: E402
from nextcommand.config import ConfigStore, Settings  # noqa: E402
from nextcommand.errors import AppError  # noqa: E402
from nextcommand.playout import Engine  # noqa: E402
from nextcommand.rundown import MEDIA_AVAILABLE, Media, Row, Rundown  # noqa: E402

CLIPS = {"VID1": ("i-1", "Apertura", 400), "VID2": ("i-2", "Deportes", 600), "VID10": ("i-3", "Antigua", 300)}


def make_rundown(*video_ids, path="SHOW.TEST"):
    rows = []
    for i, vid in enumerate(video_ids):
        media = Media()
        if vid in CLIPS:
            item, name, dur = CLIPS[vid]
            media = Media(MEDIA_AVAILABLE, item, name, dur)
        rows.append(Row(key=f"{path}/{i}", index=i, kind="story", locator=str(i), title=f"Fila {i}", video_id=vid or "",
                        media=media))
    return Rundown(path, rows)


class EngineBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeStreamIO()
        self.port = await self.fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.cfg = ConfigStore(Settings(data_dir=self.data))
        self.cfg.save({**self.cfg.data, "servers": [{"name": "Sim", "host": "127.0.0.1", "port": self.port}],
                       "allow_commands": True})
        self.engine = Engine(self.cfg, self.data, poll_interval=0.05)
        self.engine.start()

    async def asyncTearDown(self):
        await self.engine.shutdown()
        await self.fake.stop()
        self.tmp.cleanup()

    async def until(self, cond, timeout=3.0):
        end = asyncio.get_running_loop().time() + timeout
        while not cond():
            if asyncio.get_running_loop().time() > end:
                self.fail("condición no cumplida: " + repr(self.engine.snapshot()["slots"]))
            await asyncio.sleep(0.02)

    def state(self, name):
        return self.engine.slots[name].state


class EngineTests(EngineBase):
    async def test_take_alternates_and_precues(self):
        self.engine.set_rundown(make_rundown("VID1", "VID2", "VID1"))
        await self.engine.take()
        self.assertEqual(self.state("A"), "air")
        await self.until(lambda: self.state("B") == "ready")
        self.assertEqual(self.engine.slots["B"].row_key, "SHOW.TEST/1")
        await self.engine.take()
        self.assertEqual(self.state("B"), "air")
        self.assertEqual(self.state("A"), "air")  # A sigue en antena hasta que acabe
        await self.until(lambda: self.engine.slots["B"].row_key == "SHOW.TEST/1")

    async def test_stop_frees_slot_and_precue_goes_to_free(self):
        self.engine.set_rundown(make_rundown("VID1", "VID2"))
        await self.engine.take()
        await self.until(lambda: self.state("B") == "ready")
        await self.engine.stop(row_key="SHOW.TEST/0")
        self.assertEqual(self.state("A"), "idle")
        self.assertNotIn("Channel/Abort", self.fake.verbs())

    async def test_marks_go_in_playinit_not_in_clip(self):
        rd = make_rundown("VID1")
        self.engine.set_rundown(rd)
        self.engine.set_marks("SHOW.TEST/0", 50, 120)
        await self.engine.cue("A", "SHOW.TEST/0")
        cue = next(iter(self.fake.cues.values()))
        self.assertEqual((cue["In"], cue["Out"]), (50, 120))
        self.assertEqual(self.fake.items["i-1"]["In"], 0)
        self.assertNotIn("Item/Set", self.fake.verbs())
        await self.engine.take()
        await self.until(lambda: self.state("A") == "ended", 6)

    async def test_commands_disabled(self):
        self.cfg.save({**self.cfg.data, "allow_commands": False})
        self.engine.set_rundown(make_rundown("VID1"))
        with self.assertRaises(AppError) as ctx:
            await self.engine.take()
        self.assertEqual(ctx.exception.code, "commands_disabled")
        self.assertEqual([v for v in self.fake.verbs() if v.startswith("Channel/") and v != "Channel/Get"], [])

    async def test_external_change_marks_failed_and_check_recovers(self):
        self.engine.set_rundown(make_rundown("VID1", "VID2"))
        await self.engine.cue("A", "SHOW.TEST/0")
        await self.until(lambda: self.state("A") == "ready")
        self.fake.loaded.clear()  # otro cliente expulsó el cue
        self.fake.cues.clear()
        await self.until(lambda: self.state("A") == "failed")
        with self.assertRaises(AppError):
            await self.engine.take()
        await self.engine.check()
        self.assertEqual(self.state("A"), "idle")

    async def test_locked_channel_is_respected(self):
        self.engine.set_rundown(make_rundown("VID1"))
        self.fake.locked_browse = True
        with self.assertRaises(AppError) as ctx:
            await self.engine.open_browse("SHOW.TEST/0")
        self.assertEqual(ctx.exception.code, "channel_locked")

    async def test_browse_opens_and_seeks(self):
        self.engine.set_rundown(make_rundown("VID2"))
        await self.engine.open_browse("SHOW.TEST/0")
        await self.engine.browse_seek(100)
        cue = next(iter(self.fake.cues.values()))
        self.assertEqual(int(self.fake.position(cue)), 100)
        self.assertEqual(self.engine.snapshot()["browse"]["marks"], [0, 599])

    async def test_row_changed_invalidates_ready_slot(self):
        self.engine.set_rundown(make_rundown("VID1", "VID2"))
        await self.engine.cue("A", "SHOW.TEST/0")
        await self.until(lambda: self.state("A") == "ready")
        self.engine.set_rundown(make_rundown("VID2", "VID2"))
        await self.until(lambda: self.state("A") == "failed")

    async def test_restart_recovers_slots(self):
        self.engine.set_rundown(make_rundown("VID1"))
        await self.engine.cue("A", "SHOW.TEST/0")
        await self.until(lambda: self.state("A") == "ready")
        await self.engine.shutdown()
        self.engine = Engine(self.cfg, self.data, poll_interval=0.05)
        self.engine.start()
        self.engine.set_rundown(make_rundown("VID1"))
        await self.until(lambda: self.state("A") == "ready")


class TakeOnCursorTests(EngineBase):
    async def test_take_on_cursor_row_ejects_stale_precue(self):
        self.engine.set_rundown(make_rundown("VID1", "VID2", "VID10", "VID1"))
        await self.engine.take()                       # A en antena con la fila 0
        await self.until(lambda: self.state("B") == "ready")
        self.assertEqual(self.engine.slots["B"].row_key, "SHOW.TEST/1")
        await self.engine.take("SHOW.TEST/2")          # el cursor está en la fila 2
        self.assertEqual(self.engine.slots["B"].row_key, "SHOW.TEST/2")
        self.assertEqual(self.state("B"), "air")
        self.assertIn("Channel/Eject", self.fake.verbs())
        self.assertEqual(len([c for c in self.fake.cues.values() if c["channel"] == "c-out2"]), 1)
        self.assertEqual(self.engine.last_index, 2)

    async def test_precue_follows_cursor_take(self):
        self.engine.set_rundown(make_rundown("VID1", "VID2", "VID10"))
        await self.engine.take("SHOW.TEST/1")
        self.assertEqual(self.engine.slots["A"].row_key, "SHOW.TEST/1")
        await self.until(lambda: self.engine.slots["B"].row_key == "SHOW.TEST/2" and self.state("B") == "ready")

    async def test_take_row_on_air_or_offline_is_refused(self):
        self.engine.set_rundown(make_rundown("VID1", "VID2", None))
        await self.engine.take("SHOW.TEST/0")
        with self.assertRaises(AppError) as ctx:
            await self.engine.take("SHOW.TEST/0")
        self.assertEqual(ctx.exception.code, "row_on_air")
        with self.assertRaises(AppError) as ctx:
            await self.engine.take("SHOW.TEST/2")
        self.assertEqual(ctx.exception.code, "not_playable")


if __name__ == "__main__":
    unittest.main()
