from __future__ import annotations

import threading
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

    def test_reload_can_be_scheduled_from_background_thread(self) -> None:
        builds = {"count": 0}

        def factory(reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            return app

        shell = ReloadableApplication(factory)
        with TestClient(shell) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            result: dict[str, bool] = {}

            thread = threading.Thread(
                target=lambda: result.setdefault(
                    "scheduled",
                    shell.request_reload_threadsafe(),
                )
            )
            thread.start()
            thread.join(timeout=1)

            self.assertFalse(thread.is_alive())
            self.assertTrue(result["scheduled"])
            self.assertEqual(client.get("/generation").json()["generation"], 2)

    def test_update_worker_waits_for_runtime_generation_swap(self) -> None:
        builds = {"count": 0}

        def factory(_reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            return app

        shell = ReloadableApplication(factory)
        with TestClient(shell) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            outcome = {}

            def worker():
                outcome["success"] = shell.reload_updated_plugin_from_worker()

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(outcome["success"])
            self.assertEqual(client.get("/generation").json()["generation"], 2)

    def test_update_worker_reports_reload_failure_without_claiming_success(self) -> None:
        builds = {"count": 0}

        def factory(_reload_callback, _reload_status):
            builds["count"] += 1
            if builds["count"] > 1:
                raise RuntimeError("new runtime refused to start")
            return FastAPI()

        shell = ReloadableApplication(factory)
        with TestClient(shell):
            outcome = {}

            def worker():
                try:
                    shell.reload_updated_plugin_from_worker()
                except RuntimeError as exc:
                    outcome["error"] = str(exc)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertIn("Runtime 热重载失败", outcome.get("error", ""))
            self.assertIn("new runtime refused", outcome["error"])

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
