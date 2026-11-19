import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nextcommand.api import DemoSimulator, create_app  # noqa: E402
from nextcommand.config import Settings  # noqa: E402

H = {"X-Requested-By": "nextcommand", "X-Client-Id": "client-one-0001"}
H2 = {"X-Requested-By": "nextcommand", "X-Client-Id": "client-two-0002"}


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = DemoSimulator()
        self.app = create_app(Settings(data_dir=Path(self.tmp.name), poll_interval=0.05), self.sim.transport,
                              simulator=self.sim)
        self.ctx = self.app.router.lifespan_context(self.app)
        await self.ctx.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.ctx.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def post(self, path, json=None, headers=H):
        return await self.http.post(path, json=json, headers=headers)

    async def login_and_read(self, queue="PROGRAMAS.TELEDIARIO_14.ESCALETA"):
        self.assertEqual((await self.post("/api/control/claim", {})).status_code, 200)
        r = await self.post("/api/inews/login", {"server": "SIM", "user": "ana", "password": "demo"})
        self.assertEqual(r.status_code, 200, r.text)
        r = await self.post("/api/rundown/read", {"path": queue})
        self.assertEqual(r.status_code, 200, r.text)
        return (await self.http.get("/api/state", headers=H)).json()

    async def test_csrf_and_client(self):
        r = await self.http.post("/api/control/claim", json={}, headers={"X-Client-Id": H["X-Client-Id"]})
        self.assertEqual(r.status_code, 403)
        r = await self.http.post("/api/control/claim", json={}, headers={**H, "Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    async def test_requires_control(self):
        r = await self.post("/api/inews/login", {"server": "SIM", "user": "a", "password": "demo"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "not_controller")

    async def test_bad_credentials(self):
        await self.post("/api/control/claim", {})
        r = await self.post("/api/inews/login", {"server": "SIM", "user": "a", "password": "mal"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("mal", r.text)

    async def test_rundown_rows(self):
        state = await self.login_and_read()
        rows = state["rows"]
        titles = [r["title"] for r in rows]
        self.assertNotIn("CURIOSIDAD (FLOTADA)", titles)       # flotadas fuera
        self.assertEqual(state["rundown"]["floated"], 1)
        by = {r["title"]: r for r in rows}
        self.assertEqual(by["INICIO PROGRAMA"]["kind"], "break")
        self.assertEqual(by["TITULARES"]["media"], "available")
        self.assertEqual(by["PRECIO DE LA VIVIENDA"]["media"], "none")
        self.assertEqual(by["HUELGA DE TRANSPORTE"]["media"], "missing")
        self.assertTrue(by["CUMBRE EUROPEA"]["held"])
        self.assertEqual(state["inews"]["last_queue"], "PROGRAMAS.TELEDIARIO_14.ESCALETA")
        self.assertNotIn("demo", repr(state))

    async def test_take_flow_and_viewer(self):
        await self.login_and_read()
        r = await self.post("/api/playout/take")
        self.assertEqual(r.status_code, 200, r.text)
        viewer = (await self.http.get("/api/state", headers=H2)).json()
        self.assertFalse(viewer["control"]["you"])
        self.assertTrue(viewer["control"]["taken"])
        self.assertEqual(viewer["slots"]["A"]["state"], "air")
        r = await self.post("/api/playout/take", headers=H2)
        self.assertEqual(r.status_code, 403)
        r = await self.post("/api/control/claim", {}, headers=H2)
        self.assertEqual(r.status_code, 409)
        r = await self.post("/api/control/claim", {"force": True}, headers=H2)
        self.assertEqual(r.status_code, 200)
        r = await self.post("/api/playout/pause", {"slot": "A"})
        self.assertEqual(r.status_code, 403)

    async def test_config_roundtrip_and_validation(self):
        await self.post("/api/control/claim", {})
        cfg = (await self.http.get("/api/config")).json()
        cfg["outputs"]["b"]["channel"] = cfg["outputs"]["a"]["channel"]
        r = await self.http.put("/api/config", json=cfg, headers=H)
        self.assertEqual(r.status_code, 400)

    async def test_preview_and_channels(self):
        await self.login_and_read()
        r = await self.http.get("/api/preview/browse")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/jpeg"))
        r = await self.http.get("/api/streamio/channels")
        self.assertIn("OUT3", [c["short_id"] for c in r.json()["channels"]])

    async def test_marks_and_browse(self):
        state = await self.login_and_read()
        key = next(r["key"] for r in state["rows"] if r["title"] == "TITULARES")
        self.assertEqual((await self.post("/api/browse/open", {"row_key": key})).status_code, 200)
        self.assertEqual((await self.post("/api/browse/seek", {"frame": 120})).status_code, 200)
        r = await self.http.put("/api/marks", json={"row_key": key, "mark_in": 10, "mark_out": 5}, headers=H)
        self.assertEqual(r.status_code, 400)
        r = await self.http.put("/api/marks", json={"row_key": key, "mark_in": 10, "mark_out": 200}, headers=H)
        self.assertEqual(r.status_code, 200)
        state = (await self.http.get("/api/state", headers=H)).json()
        self.assertEqual(next(r for r in state["rows"] if r["key"] == key)["marks"], [10, 200])

    async def test_never_calls_forbidden_inews_ops(self):
        await self.login_and_read()
        allowed = {"Connect", "Disconnect", "GetFolderChildren", "GetQueuesForm", "SetCurrentQueue", "GetStories",
                   "HasNext", "GetStory", "GetQueueRecords"}
        self.assertTrue(set(self.sim.inews_app.state.sim.calls) <= allowed)


if __name__ == "__main__":
    unittest.main()
