import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import f0bis_inventario  # noqa: E402
from nextcommand.sim.streamio import FakeStreamIO  # noqa: E402

ALLOWED_VERBS = {"Server/List", "Item/Get", "Server/Register", "Session/Register"}


class F0bisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeStreamIO()
        self.port = await self.fake.start()
        self.out = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        await self.fake.stop()
        self.out.cleanup()

    def args(self, *extra):
        return f0bis_inventario.parse_args(["127.0.0.1", "--port", str(self.port), "--out", self.out.name, *extra])

    async def test_without_register_only_reads(self):
        report = await f0bis_inventario.run(self.args("--listen", "0"))
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["full_read"]["clips"], 3)
        self.assertTrue(all(not r["ok"] for r in report["register"]))
        verbs = set(self.fake.verbs())
        self.assertTrue(verbs <= ALLOWED_VERBS, verbs)
        self.assertEqual(len(list(Path(self.out.name).glob("f0bis-*.json"))), 2)

    async def test_register_events_and_diff(self):
        self.fake.register_supported = True

        async def change():
            await asyncio.sleep(0.3)
            self.fake.items["i-9"] = {"Name": "Nuevo", "TapeId": "VID9", "Length": 100, "In": 0, "Out": 99}
            del self.fake.items["i-3"]
            self.fake.items["i-1"] = {**self.fake.items["i-1"], "Name": "Apertura 2"}
            self.fake.push('<OCIP><Notification><Item Id="i-9"/></Notification></OCIP>')

        task = asyncio.create_task(change())
        report = await f0bis_inventario.run(self.args("--listen", "1.5", "--register-xml",
                                                      "<Server><Register><Item/></Register></Server>"))
        await task
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["register"][0]["ok"])
        self.assertEqual(report["listen"]["messages"], 1)
        self.assertIn("Notification", report["listen"]["events"][0]["xml"])
        self.assertEqual(report["diff"]["counts"], {"added": 1, "removed": 1, "changed": 1})
        saved = json.loads(next(Path(self.out.name).glob("f0bis-*[0-9].json")).read_text())
        self.assertEqual(saved["diff"]["counts"]["added"], 1)


if __name__ == "__main__":
    unittest.main()
