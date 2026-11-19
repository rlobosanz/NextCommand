"""Seguimiento de la escaleta en tiempo real contra el iNEWS y el Stream IO simulados."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nextcommand.api import DemoSimulator, create_app  # noqa: E402
from nextcommand import follow  # noqa: E402
from nextcommand.config import Settings  # noqa: E402
from nextcommand.errors import AppError  # noqa: E402
from nextcommand.sim.inews import MAIN_QUEUE  # noqa: E402

H = {"X-Requested-By": "nextcommand", "X-Client-Id": "client-one-0001"}


class FollowTests(unittest.IsolatedAsyncioTestCase):
    interval = 0.0  # sin bucle de fondo: cada prueba llama a tick() cuando quiere
    follower_module = follow

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = DemoSimulator()
        self.app = create_app(Settings(data_dir=Path(self.tmp.name), poll_interval=0.05,
                                       follow_interval=self.interval, follow_full_every=3600), self.sim.transport,
                              simulator=self.sim)
        self.ctx = self.app.router.lifespan_context(self.app)
        await self.ctx.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.engine, self.follower = self.app.state.engine, self.app.state.follower
        self.data = self.sim.inews_app.state.sim
        await self.post("/api/control/claim", {})
        r = await self.post("/api/inews/login", {"server": "SIM", "user": "ana", "password": "demo"})
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.post("/api/rundown/read", {"path": MAIN_QUEUE})
        self.assertEqual(r.status_code, 200, r.text)

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.ctx.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def post(self, path, json=None):
        return await self.http.post(path, json=json, headers=H)

    async def state(self):
        return (await self.http.get("/api/state", headers=H)).json()

    async def tick(self):
        self.data.calls.clear()
        async with self.follower.lock:
            await self.follower.tick()
        return list(self.data.calls)

    def row(self, state, title):
        return next((r for r in state["rows"] if r["title"] == title), None)

    async def until(self, predicate, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = await self.state()
            if predicate(state):
                return state
            await asyncio.sleep(0.05)
        self.fail("No se alcanzó el estado esperado: %r" % ({n: (s["state"], s["title"], s["video_id"], s["message"])
                                                            for n, s in state["slots"].items()},))

    # ---- detección de cambios ----

    async def test_nothing_changed_is_cheap(self):
        calls = await self.tick()
        self.assertEqual(set(calls), {"SetCurrentQueue", "GetQueueRecords"})
        state = await self.state()
        self.assertEqual(state["follow"]["changes"], 0)
        self.assertIn(state["follow"]["state"], ("ok", "disabled"))

    async def test_video_change_reads_only_that_story(self):
        self.data.set_video_id(MAIN_QUEUE, 3, "VID1")
        calls = await self.tick()
        self.assertEqual(calls.count("GetStory"), 1)
        self.assertNotIn("GetStories", calls)
        state = await self.state()
        self.assertEqual(self.row(state, "PLENO DEL CONGRESO")["video_id"], "VID1")
        self.assertEqual(state["follow"]["changes"], 1)

    async def test_text_only_change_is_ignored(self):
        before = await self.state()
        self.data.set_text(MAIN_QUEUE, 1, "Otro texto de la historia.")
        calls = await self.tick()
        self.assertIn("GetStory", calls)
        after = await self.state()
        self.assertEqual(after["follow"]["changes"], 0)
        self.assertEqual(after["version"], before["version"])

    async def test_move_keeps_row_identity(self):
        key = self.row(await self.state(), "LIGA: JORNADA 12")["key"]
        self.data.move(MAIN_QUEUE, 9, 2)
        calls = await self.tick()
        self.assertNotIn("GetStory", calls)  # solo cambia el orden: no hace falta releer nada
        rows = (await self.state())["rows"]
        self.assertEqual([r["title"] for r in rows][2], "LIGA: JORNADA 12")
        self.assertEqual(rows[2]["key"], key)

    async def test_new_locator_same_story_keeps_identity(self):
        key = self.row(await self.state(), "EL TIEMPO")["key"]
        self.data.queues[MAIN_QUEUE][10].locator = "777777.10.1"
        await self.tick()
        self.assertEqual(self.row(await self.state(), "EL TIEMPO")["key"], key)

    async def test_float_hold_insert_delete_title(self):
        self.data.set_flag(MAIN_QUEUE, 3, "float", True)
        self.data.set_flag(MAIN_QUEUE, 5, "hold", True)
        self.data.set_field(MAIN_QUEUE, 9, "title", "LIGA: JORNADA 13")
        self.data.insert(MAIN_QUEUE, 2, "A01B", "ÚLTIMA HORA", "VID2")
        self.data.delete(MAIN_QUEUE, 12)  # DESPEDIDA (tras la inserción)
        await self.tick()
        state = await self.state()
        titles = [r["title"] for r in state["rows"]]
        self.assertNotIn("PLENO DEL CONGRESO", titles)
        self.assertEqual(state["rundown"]["floated"], 2)
        self.assertTrue(self.row(state, "PRECIO DE LA VIVIENDA")["held"])
        self.assertIn("LIGA: JORNADA 13", titles)
        self.assertNotIn("DESPEDIDA", titles)
        self.assertEqual(titles[2], "ÚLTIMA HORA")
        self.assertEqual(self.row(state, "ÚLTIMA HORA")["media"], "available")
        self.data.set_flag(MAIN_QUEUE, 4, "float", False)  # PLENO vuelve (ahora en la posición 4)
        await self.tick()
        self.assertIsNotNone(self.row(await self.state(), "PLENO DEL CONGRESO"))

    async def test_many_changes_full_read(self):
        self.follower_module.MAX_PARTIAL = 4
        self.addCleanup(setattr, self.follower_module, "MAX_PARTIAL", 12)
        for i in range(len(self.data.queues[MAIN_QUEUE])):
            self.data.set_text(MAIN_QUEUE, i, "cambio")
        calls = await self.tick()
        self.assertIn("GetStories", calls)
        self.assertNotIn("GetStory", calls)

    async def test_reconnection_reconciles_everything(self):
        self.app.state.inews.generation += 1
        calls = await self.tick()
        self.assertIn("GetStories", calls)
        self.assertIn("GetQueueRecords", calls)

    async def test_inews_failure_marks_view_stale(self):
        self.app.state.inews.expired = True
        self.engine.watchers = 1
        self.follower.interval = 0.05
        self.follower.start()
        state = await self.until(lambda s: s["follow"]["state"] == "off")
        self.assertTrue(state["follow"]["message"])
        self.follower.checked_at = time.time() - 60
        self.follower.state = "ok"
        self.assertEqual(self.follower.public()["state"], "stale")

    # ---- reglas de directo ----

    async def take_and_precue(self):
        r = await self.post("/api/playout/take")
        self.assertEqual(r.status_code, 200, r.text)
        return await self.until(lambda s: s["slots"]["A"]["state"] == "air" and s["slots"]["B"]["state"] == "ready")

    async def test_on_air_row_is_never_interrupted(self):
        state = await self.take_and_precue()
        self.assertEqual(state["slots"]["A"]["title"], "TITULARES")
        self.data.set_video_id(MAIN_QUEUE, 1, "VID2")
        await self.tick()
        state = await self.state()
        self.assertEqual(state["slots"]["A"]["state"], "air")
        self.assertEqual(state["slots"]["A"]["video_id"], "VID1")
        self.assertIn("VideoID", state["slots"]["A"]["message"])
        self.data.delete(MAIN_QUEUE, 1)
        await self.tick()
        self.assertEqual((await self.state())["slots"]["A"]["state"], "air")

    async def test_ready_row_video_change_recues(self):
        state = await self.take_and_precue()
        self.assertEqual((state["slots"]["B"]["title"], state["slots"]["B"]["video_id"]), ("INCENDIO EN EL PUERTO", "VID2"))
        self.data.set_video_id(MAIN_QUEUE, 2, "VID10")
        await self.tick()
        self.assertEqual(self.engine.slots["B"].state, "failed")  # mientras se vuelve a resolver no hay Take
        with self.assertRaises(AppError):
            await self.engine.take()
        state = await self.until(lambda s: s["slots"]["B"]["state"] == "ready" and s["slots"]["B"]["video_id"] == "VID10")
        self.assertEqual(state["slots"]["B"]["title"], "INCENDIO EN EL PUERTO")

    async def test_ready_row_deleted_moves_to_next(self):
        await self.take_and_precue()
        self.data.delete(MAIN_QUEUE, 2)  # INCENDIO, preparada en B
        await self.tick()
        state = await self.until(lambda s: s["slots"]["B"]["state"] == "ready"
                                 and s["slots"]["B"]["title"] == "PLENO DEL CONGRESO")
        self.assertEqual(state["slots"]["A"]["state"], "air")

    async def test_insert_before_precued_row_repoints_precue(self):
        await self.take_and_precue()
        self.data.insert(MAIN_QUEUE, 2, "A01B", "ÚLTIMA HORA", "VID10")
        await self.tick()
        state = await self.until(lambda s: s["slots"]["B"]["state"] == "ready" and s["slots"]["B"]["title"] == "ÚLTIMA HORA")
        self.assertEqual(state["slots"]["A"]["state"], "air")


class FollowLoopTests(FollowTests):
    """Las mismas pruebas con el bucle de fondo activo (comprueba que no se pisan)."""
    interval = 0.1

    async def test_background_loop_applies_changes(self):
        self.engine.watchers = 1
        self.data.set_video_id(MAIN_QUEUE, 3, "VID2")
        state = await self.until(lambda s: self.row(s, "PLENO DEL CONGRESO")["video_id"] == "VID2")
        self.assertEqual(state["follow"]["state"], "ok")
        self.assertGreaterEqual(state["follow"]["changes"], 1)


if __name__ == "__main__":
    unittest.main()
