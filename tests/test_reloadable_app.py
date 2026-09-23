from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuckclassroom.web.reloadable import ReloadableApplication


class ReloadableApplicationTests(unittest.TestCase):
    def test_reload_swaps_generation_without_restarting_process(self) -> None:
        builds = {"count": 0}

        def factory(reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            @app.post("/reload")
            async def reload_generation():
                scheduled = await reload_callback()
                return {"scheduled": scheduled}

            return app

        app = ReloadableApplication(factory)
        with TestClient(app) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            self.assertTrue(client.post("/reload").json()["scheduled"])
            self.assertEqual(client.get("/generation").json()["generation"], 2)

    def test_failed_reload_keeps_previous_generation(self) -> None:
        builds = {"count": 0}
        shell = None

        def factory(reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            if generation == 2:
                raise RuntimeError("boom")
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            @app.post("/reload")
            async def reload_generation():
                return {"scheduled": await reload_callback()}

            return app

        shell = ReloadableApplication(factory)
        with TestClient(shell) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            self.assertTrue(client.post("/reload").json()["scheduled"])
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            self.assertIn("boom", str(shell.runtime_status()["last_error"]))


if __name__ == "__main__":
    unittest.main()
