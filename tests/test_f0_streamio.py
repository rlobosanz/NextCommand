import sys
import time
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import f0_streamio  # noqa: E402
from fake_streamio import FakeStreamIO  # noqa: E402

READ_VERBS = {"Server/List", "Channel/Get", "Item/Get"}


class F0Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeStreamIO()
        self.port = await self.fake.start()
        self.out = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        await self.fake.stop()
        self.out.cleanup()

    def args(self, *extra):
        return f0_streamio.parse_args(["127.0.0.1", "--port", str(self.port), "--playlist", "OUT1", "OUT2",
                                       "--browse", "OUT3", "--tape-id", "VID1", "--tape-id-b", "VID2",
                                       "--samples", "5", "--play-frames", "25", "--max-wait", "10",
                                       "--out", self.out.name, *extra])

    async def test_read_phase_only_reads(self):
        report = await f0_streamio.run(self.args("--inventory"))
        self.assertEqual(report["errors"], [])
        self.assertTrue(set(self.fake.verbs()) <= READ_VERBS, self.fake.verbs())
        read = report["read"]
        self.assertEqual(read["channels"]["browse"]["review"], True)
        self.assertEqual(read["channels"]["A"]["end_of_clip"]["IsAutoStillEnabled"], "True")
        self.assertTrue(read["parallel"]["ok"])
        self.assertEqual(read["status_latency_ms"]["n"], 5)
        tapes = {t["tape_id"]: t for t in read["tapes"]}
        self.assertEqual(tapes["VID1"]["exact"], 1)        # VID10 no cuenta como VID1
        self.assertEqual(tapes["VID1"]["relation"], "Equal")
        self.assertEqual(read["inventory"]["items"], 3)
        self.assertIn("Thumbnail", read["thumbnail_candidates"])
        self.assertTrue(all(p["ok"] for p in read["previews"].values()))
        self.assertEqual(len(list(Path(self.out.name).glob("f0-*.json"))), 1)

    async def test_exact_search_falls_back_to_contain(self):
        self.fake.equal_supported = False
        report = await f0_streamio.run(self.args())
        tapes = {t["tape_id"]: t for t in report["read"]["tapes"]}
        self.assertEqual(tapes["VID1"]["relation"], "Contain")
        self.assertEqual(tapes["VID1"]["returned"], 2)
        self.assertEqual(tapes["VID1"]["exact"], 1)

    async def test_unknown_channel_stops_before_anything_else(self):
        with self.assertRaises(SystemExit):
            await f0_streamio.run(f0_streamio.parse_args(
                ["127.0.0.1", "--port", str(self.port), "--playlist", "OUT1", "OUT9", "--browse", "OUT3",
                 "--out", self.out.name]))

    async def test_control_phase(self):
        report = await f0_streamio.run(self.args("--control", "--yes"))
        self.assertEqual(report["errors"], [])
        steps = report["control"]["steps"]
        self.assertTrue(steps["cue_A"]["handle"])
        self.assertIsNotNone(steps["cue_A"]["ready_ms"])
        self.assertEqual(steps["cue_in_out"]["cue_now"], [0, 399])  # como 10.0.0.26: Set aceptado e ignorado
        self.assertTrue(steps["cue_in_out"]["clip_unchanged"])
        self.assertIsNotNone(steps["take_A"]["confirmed_ms"])
        self.assertTrue(steps["cue_B_during_A"]["handle"])
        self.assertGreaterEqual(steps["end_of_A"]["transitions"][-1]["position"], 75)
        self.assertEqual(steps["stop_B"]["status"]["speed"], 0)
        self.assertTrue(steps["reconnect"]["same_handle"])
        self.assertTrue(steps["browse"]["preview"]["ok"])
        # Nunca escribe en clips, ni bloquea, ni aborta; deja vacíos los canales que estaban vacíos.
        verbs = set(self.fake.verbs())
        clip_sets = [r for r in self.fake.received if r.find("Item") is not None and r.find("Item").get("Id")
                     and r.find("Item/Set") is not None]
        self.assertEqual(clip_sets, [])
        self.assertFalse(any(v.endswith(("/Abort", "/Lock", "/Delete")) for v in verbs))
        self.assertEqual({c["Mode"] for c in self.fake.channels.values()}, {"Empty"})

    async def test_active_cue_is_the_playing_one_not_the_first(self):
        # As seen on 10.0.0.26: a Ready cue listed before the one on air.
        self.fake.cues["p-wait"] = {"channel": "c-out1", "item": "i-2", "In": 0, "Out": 599, "base": 0,
                                    "speed": 0, "since": time.monotonic()}
        report = await f0_streamio.run(self.args("--control", "--yes"))
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["control"]["before"]["A"]["handle"], "")
        self.assertEqual(len(report["control"]["before"]["A"]["cues"]), 1)
        self.assertEqual(report["control"]["steps"]["take_A"]["status"]["handle"],
                         report["control"]["steps"]["cue_A"]["handle"])

    async def test_locked_browse_channel_can_be_skipped(self):
        self.fake.locked_browse = True
        with self.assertRaises(SystemExit):
            await f0_streamio.run(self.args("--control", "--yes"))
        report = await f0_streamio.run(self.args("--control", "--yes", "--skip-browse"))
        self.assertEqual(report["errors"], [])
        self.assertNotIn("browse", report["control"]["steps"])

    async def test_inout_variants(self):
        report = await f0_streamio.run(self.args("--control", "--yes", "--inout-only"))
        self.assertEqual(report["errors"], [])
        self.assertIsNone(report["control"])
        variants = report["inout"]["variants"]
        self.assertEqual(report["inout"]["requested"], [50, 75])
        self.assertTrue(variants["set_before_play"]["respects_in"])
        self.assertTrue(variants["set_before_play"]["respects_out"])
        self.assertTrue(variants["item_with_marks"]["respects_out"])
        self.assertFalse(variants["playinit_with_marks"]["respects_out"])
        self.assertTrue(variants["seek_to_in"]["respects_in"])
        self.assertTrue(report["inout"]["clip_unchanged"])
        self.assertEqual({c["Mode"] for c in self.fake.channels.values()}, {"Empty"})
        self.assertFalse(any(r.find("PlayInit") is not None for r in self.fake.received
                             if r.find("Channel") is not None and r.find("Channel").get("Id") != "c-out1"))

    async def test_control_refuses_a_channel_that_is_playing(self):
        self.fake.cues["p-x"] = {"channel": "c-out2", "item": "i-2", "In": 0, "Out": 599, "base": 0,
                                 "speed": 100, "since": time.monotonic()}
        self.fake.loaded["c-out2"] = "p-x"
        self.fake.channels["c-out2"]["Mode"] = "Play"
        with self.assertRaises(SystemExit):
            await f0_streamio.run(self.args("--control", "--yes"))
        self.assertFalse(any(v in ("Channel/PlayInit", "Channel/Play", "Channel/Set") for v in self.fake.verbs()))


if __name__ == "__main__":
    unittest.main()
