"""MacSafe logic tests. Run: /usr/bin/python3 -m unittest discover -s tests -v

The tests do not touch the user's real data: paths are replaced with temporary folders,
launchctl and ioreg are not called.
"""
import importlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def setUpModule():
    # Old tests check Ukrainian texts; the default language is now English
    import i18n
    i18n.set_language("uk")


class PrefsTests(unittest.TestCase):
    def setUp(self):
        import prefs
        self.prefs = prefs
        self.dir = tempfile.mkdtemp(prefix="macsafe-prefs-")
        self.path = os.path.join(self.dir, "налаштування.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_defaults_when_no_file(self):
        p = self.prefs.Prefs(self.path)
        self.assertEqual((p.get("cpu"), p.get("ram"), p.get("gpu"), p.get("style")), (True, True, True, "both"))

    def test_roundtrip_persists(self):
        p = self.prefs.Prefs(self.path)
        p.set("gpu", False)
        p.set("style", "text")
        q = self.prefs.Prefs(self.path)
        self.assertFalse(q.get("gpu"))
        self.assertEqual(q.get("style"), "text")

    def test_invalid_style_rejected(self):
        with self.assertRaises(ValueError):
            self.prefs.Prefs(self.path).set("style", "neon")

    def test_unknown_key_rejected(self):
        with self.assertRaises(KeyError):
            self.prefs.Prefs(self.path).set("cpu_temp", True)

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{не json")
        self.assertEqual(self.prefs.Prefs(self.path).get("style"), "both")

    def test_wrong_types_are_ignored(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"cpu": "так", "style": "neon", "ram": False}, fh)
        p = self.prefs.Prefs(self.path)
        self.assertTrue(p.get("cpu"))
        self.assertEqual(p.get("style"), "both")
        self.assertFalse(p.get("ram"))


class IntervalPrefTests(PrefsTests):
    def test_interval_default_and_roundtrip(self):
        p = self.prefs.Prefs(self.path)
        self.assertEqual(p.get("interval"), 2)
        p.set("interval", 5)
        self.assertEqual(self.prefs.Prefs(self.path).get("interval"), 5)

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            self.prefs.Prefs(self.path).set("interval", 3)

    def test_bad_interval_in_file_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"interval": 60}, fh)
        self.assertEqual(self.prefs.Prefs(self.path).get("interval"), 2)

    def test_monitor_interval_changes(self):
        import metrics
        m = metrics.Monitor(interval=1.0)
        m.set_interval(2)
        self.assertEqual(m._interval, 2.0)
        m.set_interval(0)
        self.assertEqual(m._interval, 2.0)


class ParseGpuTests(unittest.TestCase):
    def setUp(self):
        import metrics
        self.parse = metrics.parse_gpu

    def _ioreg(self, *stats_list):
        return plistlib.dumps([{"PerformanceStatistics": s} for s in stats_list], fmt=plistlib.FMT_XML)

    def test_device_utilization_key(self):
        self.assertEqual(self.parse(self._ioreg({"Device Utilization %": 37})), 37.0)

    def test_fallback_key(self):
        self.assertEqual(self.parse(self._ioreg({"GPU Activity(%)": 12})), 12.0)

    def test_max_across_devices_not_sum(self):
        raw = self._ioreg({"Device Utilization %": 20}, {"Device Utilization %": 55})
        self.assertEqual(self.parse(raw), 55.0)

    def test_missing_key_is_none(self):
        self.assertIsNone(self.parse(self._ioreg({"Renderer Utilization %": 9})))

    def test_bool_is_not_a_number(self):
        self.assertIsNone(self.parse(self._ioreg({"Device Utilization %": True})))

    def test_clamped_to_range(self):
        self.assertEqual(self.parse(self._ioreg({"Device Utilization %": 250})), 100.0)

    def test_garbage_and_empty_are_none(self):
        self.assertIsNone(self.parse(b"not a plist"))
        self.assertIsNone(self.parse(b""))
        self.assertIsNone(self.parse(None))


class AutostartTests(unittest.TestCase):
    def setUp(self):
        import autostart
        self.autostart = autostart
        self.dir = tempfile.mkdtemp(prefix="macsafe-agent-")
        self.saved = (autostart.AGENT_DIR, autostart.AGENT, autostart._is_loaded, autostart._launchctl)
        autostart.AGENT_DIR = self.dir
        autostart.AGENT = os.path.join(self.dir, autostart.LABEL + ".plist")
        self.calls = []
        autostart._launchctl = lambda *a: (self.calls.append(a) or (True, ""))
        self.loaded = False
        autostart._is_loaded = lambda: self.loaded

    def tearDown(self):
        a = self.autostart
        a.AGENT_DIR, a.AGENT, a._is_loaded, a._launchctl = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("XPC_SERVICE_NAME", None)

    def test_plist_has_background_flag_and_no_keepalive(self):
        self.autostart.enable(["/x/launcher"], "/x", "/x/log")
        with open(self.autostart.AGENT, "rb") as fh:
            p = plistlib.load(fh)
        self.assertEqual(p["ProgramArguments"], ["/x/launcher", "--background"])
        self.assertTrue(p["RunAtLoad"])
        self.assertNotIn("KeepAlive", p)

    def test_enable_bootstraps_when_not_loaded(self):
        self.autostart.enable(["/x/launcher"], "/x", "/x/log")
        self.assertEqual([c[0] for c in self.calls], ["bootstrap"])

    def test_changed_plist_reloads_when_loaded(self):
        self.loaded = True
        self.autostart.enable(["/old/launcher"], "/x", "/x/log")
        self.calls.clear()
        self.autostart.enable(["/new/launcher"], "/x", "/x/log")
        self.assertEqual([c[0] for c in self.calls], ["bootout", "bootstrap"])

    def test_same_plist_does_not_reload(self):
        self.loaded = True
        self.autostart.enable(["/x/launcher"], "/x", "/x/log")
        self.calls.clear()
        self.autostart.enable(["/x/launcher"], "/x", "/x/log")
        self.assertEqual(self.calls, [])

    def test_disable_removes_plist_and_boots_out(self):
        self.autostart.enable(["/x/launcher"], "/x", "/x/log")
        self.loaded = True
        self.calls.clear()
        self.autostart.disable()
        self.assertFalse(self.autostart.is_enabled())
        self.assertEqual([c[0] for c in self.calls], ["bootout"])

    def test_self_job_never_boots_itself_out(self):
        self.autostart.enable(["/x/launcher"], "/x", "/x/log")
        self.loaded = True
        os.environ["XPC_SERVICE_NAME"] = self.autostart.LABEL
        self.calls.clear()
        self.autostart.disable()
        self.assertEqual(self.calls, [])


class CleanupTests(unittest.TestCase):
    """cleanup reads HOME on import, so the module is reloaded with a temporary home."""

    def setUp(self):
        # realpath: on macOS /var is a symlink to /private/var; cleaning refuses paths that go through a link
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="macsafe-home-"))
        self.saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        caches = os.path.join(self.home, "Library", "Caches")
        os.makedirs(os.path.join(caches, "testapp", "sub"))
        os.makedirs(os.path.join(caches, "com.apple.Safari"))
        os.makedirs(os.path.join(self.home, "outside"))
        with open(os.path.join(caches, "testapp", "a.bin"), "wb") as fh:
            fh.write(b"x" * 300_000)
        with open(os.path.join(caches, "testapp", "sub", "b.bin"), "wb") as fh:
            fh.write(b"x" * 200_000)
        # Cleaning touches only files older than the threshold: old a.bin and b.bin, the fresh fresh.bin stays
        for rel in (("testapp", "a.bin"), ("testapp", "sub", "b.bin")):
            stamp = time.time() - 90 * 86400
            os.utime(os.path.join(caches, *rel), (stamp, stamp))
        with open(os.path.join(caches, "testapp", "fresh.bin"), "wb") as fh:
            fh.write(b"f" * 100_000)
        with open(os.path.join(caches, "com.apple.Safari", "keep"), "w") as fh:
            fh.write("keep")
        with open(os.path.join(self.home, "outside", "secret"), "w") as fh:
            fh.write("secret")
        os.symlink(os.path.join(self.home, "outside"), os.path.join(caches, "linkapp"))
        import cleanup
        self.cleanup = importlib.reload(cleanup)
        self.caches = caches

    def tearDown(self):
        if self.saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.saved_home
        shutil.rmtree(self.home, ignore_errors=True)
        importlib.reload(self.cleanup)

    def test_scan_lists_app_cache_but_not_apple_or_links(self):
        ids = [i["id"] for i in self.cleanup.scan()["items"]]
        self.assertIn("cache:testapp", ids)
        self.assertNotIn("cache:com.apple.Safari", ids)
        self.assertNotIn("cache:linkapp", ids)

    def test_clean_removes_only_old_files_keeps_folder(self):
        result = self.cleanup.clean(["cache:testapp"])
        self.assertTrue(result["ok"])
        self.assertTrue(os.path.isdir(os.path.join(self.caches, "testapp")))
        self.assertEqual(os.listdir(os.path.join(self.caches, "testapp")), ["fresh.bin"])
        self.assertGreater(result["freedMb"], 0.4)

    def test_scan_size_counts_only_old_files(self):
        item = next(i for i in self.cleanup.scan()["items"] if i["id"] == "cache:testapp")
        self.assertAlmostEqual(item["sizeMb"], 0.5, delta=0.05)

    def test_clean_refuses_traversal_apple_symlink_unknown(self):
        for bad in ("cache:../../outside", "cache:com.apple.Safari", "cache:linkapp", "rm -rf /"):
            with self.subTest(bad=bad):
                r = self.cleanup.clean([bad])["results"][0]
                self.assertIn("error", r)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "outside", "secret")))
        self.assertTrue(os.path.isfile(os.path.join(self.caches, "com.apple.Safari", "keep")))

    def test_clean_requires_nonempty_list(self):
        self.assertFalse(self.cleanup.clean([])["ok"])
        self.assertFalse(self.cleanup.clean("cache:testapp")["ok"])

    def _make_cache(self, name):
        path = os.path.join(self.caches, name)
        os.makedirs(path)
        old = os.path.join(path, "old.bin")
        with open(old, "wb") as fh:
            fh.write(b"x" * 100_000)
        stamp = time.time() - 90 * 86400
        os.utime(old, (stamp, stamp))
        return old

    def test_running_app_cache_is_not_cleaned(self):
        old = self._make_cache("com.vendor.Editor")
        self.cleanup.running_apps = lambda: ({"com.vendor.editor"}, set())
        item = next(i for i in self.cleanup.scan()["items"] if i["id"] == "cache:com.vendor.Editor")
        self.assertTrue(item["blocked"])
        r = self.cleanup.clean(["cache:com.vendor.Editor"])["results"][0]
        self.assertTrue(r.get("skipped"))
        self.assertTrue(os.path.isfile(old))

    def test_running_app_by_name_is_not_cleaned(self):
        old = self._make_cache("Figma")
        self.cleanup.running_apps = lambda: (set(), {"figma"})
        r = self.cleanup.clean(["cache:Figma"])["results"][0]
        self.assertTrue(r.get("skipped"))
        self.assertTrue(os.path.isfile(old))

    def test_cache_named_by_vendor_or_bundle_tail_is_matched_to_running_app(self):
        self.cleanup.running_apps = lambda: (set(), {"google chrome", "codex"})
        self.assertTrue(self.cleanup.blocked_reason("cache:Google"))
        self.assertTrue(self.cleanup.blocked_reason("cache:com.openai.codex"))
        self.assertIsNone(self.cleanup.blocked_reason("cache:Goo"))
        self.assertIsNone(self.cleanup.blocked_reason("cache:com.vendor.other"))

    def test_protected_cache_is_never_cleaned(self):
        old = self._make_cache("com.spotify.client")
        self.cleanup.running_apps = lambda: (set(), set())
        item = next(i for i in self.cleanup.scan()["items"] if i["id"] == "cache:com.spotify.client")
        self.assertTrue(item["blocked"])
        r = self.cleanup.clean(["cache:com.spotify.client"])["results"][0]
        self.assertTrue(r.get("skipped"))
        self.assertTrue(os.path.isfile(old))

    def test_idle_app_cache_is_cleaned_and_scan_counts_files(self):
        self.cleanup.running_apps = lambda: (set(), set())
        item = next(i for i in self.cleanup.scan()["items"] if i["id"] == "cache:testapp")
        self.assertIsNone(item["blocked"])
        self.assertEqual(item["files"], 2)
        self.assertGreater(self.cleanup.clean(["cache:testapp"])["freedMb"], 0.4)

    def test_xcode_cache_blocked_while_xcode_runs(self):
        self.cleanup.running_apps = lambda: ({"com.apple.dt.xcode"}, set())
        self.assertTrue(self.cleanup.blocked_reason("xcode"))
        self.cleanup.running_apps = lambda: (set(), set())
        self.assertIsNone(self.cleanup.blocked_reason("xcode"))


class SecuritySummaryTests(unittest.TestCase):
    def setUp(self):
        import engine
        self.app = engine
        self.dir = tempfile.mkdtemp(prefix="macsafe-data-")
        os.makedirs(os.path.join(self.dir, "дані"))
        self.saved = engine.DATA_DIR
        engine.DATA_DIR = self.dir

    def tearDown(self):
        self.app.DATA_DIR = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def _report(self, name, kinds):
        items = [{"kind": k, "label": f"x{i}", "value": "v"} for i, k in enumerate(kinds)]
        with open(os.path.join(self.dir, "дані", name), "w", encoding="utf-8") as fh:
            json.dump({"security": items}, fh, ensure_ascii=False)

    def test_no_report_is_none(self):
        self.assertEqual(self.app.security_summary()["level"], "none")

    def test_ok_when_only_good_and_info(self):
        self._report("report-2026-10-10.json", ["good", "info", "good"])
        s = self.app.security_summary()
        self.assertEqual((s["level"], s["issues"]), ("ok", 0))
        self.assertEqual(s["checkedAt"], "2026-10-10")

    def test_warning_counted(self):
        self._report("report-2026-10-10.json", ["good", "warning"])
        s = self.app.security_summary()
        self.assertEqual((s["level"], s["issues"], s["critical"]), ("warning", 1, 0))

    def test_critical_wins_and_counts_both(self):
        self._report("report-2026-10-10.json", ["critical", "warning", "warning"])
        s = self.app.security_summary()
        self.assertEqual((s["level"], s["issues"], s["critical"]), ("critical", 3, 1))

    def test_uses_latest_report(self):
        self._report("report-2026-10-09.json", ["critical"])
        self._report("report-2026-10-10.json", ["good"])
        self.assertEqual(self.app.security_summary()["level"], "ok")

    def test_broken_json_is_none(self):
        with open(os.path.join(self.dir, "дані", "report-2026-10-10.json"), "w") as fh:
            fh.write("{")
        self.assertEqual(self.app.security_summary()["level"], "none")


class DiffTests(unittest.TestCase):
    def setUp(self):
        from diff import security_changes
        self.changes = security_changes

    def test_no_change_is_empty(self):
        items = [{"label": "FileVault", "kind": "good", "value": "Увімкнено"}]
        self.assertEqual(self.changes(items, items), [])

    def test_value_change_detected(self):
        old = [{"label": "Брандмауер", "kind": "good", "value": "Увімкнено"}]
        new = [{"label": "Брандмауер", "kind": "warning", "value": "Вимкнено"}]
        self.assertEqual(self.changes(old, new),
                         [{"label": "Брандмауер", "from": "Увімкнено · ок", "to": "Вимкнено · увага"}])

    def test_new_and_removed_checks(self):
        old = [{"label": "A", "kind": "good", "value": "1"}]
        new = [{"label": "B", "kind": "good", "value": "2"}]
        got = sorted(self.changes(old, new), key=lambda c: c["label"])
        self.assertEqual(got, [{"label": "A", "from": "1 · ок", "to": None},
                               {"label": "B", "from": None, "to": "2 · ок"}])

    def test_bad_items_ignored(self):
        self.assertEqual(self.changes(None, [{"value": "без назви"}, "не словник"]), [])


class SecuritySummaryChangesTests(SecuritySummaryTests):
    def test_changes_against_previous_report(self):
        self._report("report-2026-10-09.json", ["good"])
        with open(os.path.join(self.dir, "дані", "report-2026-10-10.json"), "w", encoding="utf-8") as fh:
            json.dump({"security": [{"kind": "warning", "label": "x0", "value": "v"}]}, fh)
        s = self.app.security_summary()
        self.assertEqual(s["changeCount"], 1)
        self.assertEqual(s["changes"][0]["label"], "x0")

    def test_first_report_has_no_changes(self):
        self._report("report-2026-10-10.json", ["good"])
        self.assertEqual(self.app.security_summary()["changeCount"], 0)


class PluralTests(unittest.TestCase):
    def tearDown(self):
        import i18n
        i18n.set_language("uk")

    def test_ukrainian_plural(self):
        import i18n
        i18n.set_language("uk")
        cases = {1: "проблема", 2: "проблеми", 4: "проблеми", 5: "проблем", 11: "проблем",
                 12: "проблем", 21: "проблема", 22: "проблеми", 112: "проблем", 101: "проблема"}
        for n, word in cases.items():
            with self.subTest(n=n):
                self.assertEqual(i18n.plural_problem(n), word)

    def test_english_plural(self):
        import i18n
        i18n.set_language("en")
        self.assertEqual(i18n.plural_problem(1), "problem")
        self.assertEqual(i18n.plural_problem(2), "problems")


class UndefinedNamesTests(unittest.TestCase):
    """Checks that every name a module uses is defined or imported in it (catches a forgotten import of T)."""

    def test_no_undefined_names(self):
        import ast
        import builtins
        import glob
        root = os.path.join(os.path.dirname(__file__), "..")
        problems = []
        for path in glob.glob(os.path.join(root, "*.py")) + glob.glob(os.path.join(root, "packaging", "*.py")):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            defined = set(dir(builtins)) | {"__file__", "__name__"}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.arg):
                    defined.add(node.arg)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    defined.add(node.id)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    defined.add(node.name)
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            missing = sorted(used - defined)
            if missing:
                problems.append((os.path.basename(path), missing))
        self.assertEqual(problems, [])


class I18nTests(unittest.TestCase):
    def tearDown(self):
        import i18n
        i18n.set_language("uk")

    def test_ukrainian_is_default_and_returns_source(self):
        import i18n
        i18n.set_language("uk")
        self.assertEqual(i18n.T("Звільнити памʼять"), "Звільнити памʼять")

    def test_english_translates_and_formats(self):
        import i18n
        i18n.set_language("en")
        self.assertEqual(i18n.T("Звільнити памʼять"), "Free memory")
        self.assertEqual(i18n.T("Вільно {free:.1f} ГБ. Спробуйте «Детальне очищення».", free=2.5),
                         "2.5 GB free. Try “Detailed clean”.")

    def test_missing_key_falls_back_to_source(self):
        import i18n
        i18n.set_language("en")
        self.assertEqual(i18n.T("Текст, якого немає у словнику"), "Текст, якого немає у словнику")

    def test_load_defaults_to_english(self):
        import i18n
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "немає.json")
            self.assertEqual(i18n.load(missing), "en")
            path = os.path.join(tmp, "налаштування.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"language": "uk"}, fh)
            self.assertEqual(i18n.load(path), "uk")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"language": "de"}, fh)
            self.assertEqual(i18n.load(path), "en")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("не json")
            self.assertEqual(i18n.load(path), "en")

    def test_every_translation_keeps_placeholders(self):
        import i18n
        for source, english in i18n.EN.items():
            with self.subTest(source=source):
                self.assertEqual(set(re.findall(r"\{(\w+)", source)), set(re.findall(r"\{(\w+)", english)))

    def test_every_t_call_in_code_has_a_translation(self):
        import ast
        import glob
        import i18n
        root = os.path.join(os.path.dirname(__file__), "..")
        missing = []
        for path in glob.glob(os.path.join(root, "*.py")):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "T" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value not in i18n.EN:
                        missing.append((os.path.basename(path), arg.value))
        self.assertEqual(missing, [])

    def test_window_language_request_is_written_to_a_file(self):
        import engine
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".lang-request")
            original = engine.LANG_REQUEST
            engine.LANG_REQUEST = path
            try:
                self.assertTrue(engine.Api().set_language("uk")["ok"])
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "uk")
                self.assertFalse(engine.Api().set_language("de")["ok"])
            finally:
                engine.LANG_REQUEST = original

    def test_snapshot_export_saves_into_downloads_without_overwriting(self):
        import engine
        with tempfile.TemporaryDirectory() as home, unittest.mock.patch.dict(os.environ, {"HOME": home}):
            api = engine.Api()
            first = api.save_snapshot("аудит-mac.json", '{"a": 1}')
            second = api.save_snapshot("аудит-mac.json", '{"a": 2}')
            self.assertTrue(first["ok"] and second["ok"])
            self.assertNotEqual(first["name"], second["name"])
            with open(first["path"], encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '{"a": 1}')
            self.assertTrue(first["path"].startswith(os.path.join(home, "Downloads")))
            # шлях із папками не виходить за межі «Завантаження»
            traversal = api.save_snapshot("../../evil.json", "{}")
            self.assertTrue(traversal["path"].startswith(os.path.join(home, "Downloads")))

    def test_language_pref_accepts_only_known_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = importlib.import_module("prefs").Prefs(os.path.join(tmp, "налаштування.json"))
            self.assertEqual(prefs.get("language"), "en")
            prefs.set("language", "en")
            self.assertEqual(importlib.import_module("prefs").Prefs(os.path.join(tmp, "налаштування.json")).get("language"), "en")
            with self.assertRaises(ValueError):
                prefs.set("language", "de")


class MonitorScanTests(unittest.TestCase):
    """The process list is scanned only when it is open: a closed agent does not touch processes."""

    def setUp(self):
        import metrics
        self.metrics = metrics
        self.calls = 0
        self._orig = metrics.take_processes

        def counting():
            self.calls += 1
            return [], [], [], []
        metrics.take_processes = counting

    def tearDown(self):
        self.metrics.take_processes = self._orig

    def test_no_process_scan_while_closed(self):
        import time
        mon = self.metrics.Monitor(interval=0.05)
        mon.start()
        try:
            time.sleep(0.4)
            self.assertEqual(self.calls, 0)
            self.assertIsNotNone(mon.latest())
        finally:
            mon.stop()

    def test_last_list_kept_after_close(self):
        import time
        group = {"name": "claude", "cpu": 1.0, "rss": 10, "gpu": None, "count": 1,
                 "pids": [1], "all": [1], "killable": True}
        self.metrics.take_processes = lambda: ([group], [group], [], [group])
        mon = self.metrics.Monitor(interval=0.05)
        mon.start()
        try:
            mon.set_detail(True)
            time.sleep(0.5)
            mon.set_detail(False)
            time.sleep(0.2)
            self.assertEqual([g["name"] for g in mon.latest()["topRam"]], ["claude"])
        finally:
            mon.stop()

    def test_scan_resumes_when_opened_and_stops_when_closed(self):
        import time
        mon = self.metrics.Monitor(interval=0.05)
        mon.start()
        try:
            mon.set_detail(True)
            time.sleep(0.4)
            self.assertGreaterEqual(self.calls, 2)   # priming and at least one real measurement
            mon.set_detail(False)
            time.sleep(0.1)
            stopped_at = self.calls
            time.sleep(0.3)
            self.assertEqual(self.calls, stopped_at)
        finally:
            mon.stop()


class BundlePathTests(unittest.TestCase):
    def test_app_bundle_from_executable_path(self):
        import metrics
        exe = "/Applications/Ghostty.app/Contents/MacOS/ghostty"
        self.assertEqual(metrics.bundle_path(exe), "/Applications/Ghostty.app")

    def test_plain_binary_has_no_bundle(self):
        import metrics
        self.assertIsNone(metrics.bundle_path("/usr/bin/yes"))
        self.assertIsNone(metrics.bundle_path(""))


class CoreTypesTests(unittest.TestCase):
    def test_pairs_cluster_type_with_logical_cpu_in_same_node(self):
        import metrics
        sample = """
+-o cpu0  <class X>
    | |   "cluster-type" = <"E">
    | |   "logical-cpu-id" = 0
+-o cpu6  <class X>
    | |   "cluster-type" = <"P">
    | |   "logical-cpu-id" = 6
"""
        self.assertEqual(metrics.parse_core_types(sample), ["E", "P"])

    def test_empty_output_gives_no_types(self):
        import metrics
        self.assertEqual(metrics.parse_core_types(""), [])


class QuickMetricsTests(unittest.TestCase):
    def test_per_core_loads_cover_every_core(self):
        import metrics
        import psutil
        snap = metrics.take_quick()
        self.assertEqual(len(snap["perCore"]), psutil.cpu_count())



class HistoryTests(unittest.TestCase):
    def setUp(self):
        import history
        self.h = history
        self.tmp = tempfile.mkdtemp(prefix="macsafe-hist-")
        self.path = os.path.join(self.tmp, "history.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, temps, start=1_000_000):
        return [{"t": start + i * 60, "cpu": c} for i, c in enumerate(temps)]

    def test_append_and_load_roundtrip_and_skip_broken_lines(self):
        self.h.append({"t": 100, "cpu": 50.0}, self.path, now=100)
        with open(self.path, "a") as fh:
            fh.write("not json\n")
        self.h.append({"t": 160, "cpu": 51.0}, self.path, now=160)
        self.assertEqual([r["cpu"] for r in self.h.load(0, self.path)], [50.0, 51.0])

    def test_load_returns_only_the_requested_window(self):
        for t in (1000, 5000, 90000):
            self.h.append({"t": t, "cpu": 40.0}, self.path, now=90000)
        self.assertEqual([r["t"] for r in self.h.load(1, self.path, now=90000)], [90000])

    def test_old_samples_are_trimmed_when_the_file_is_large(self):
        old = {"t": 10, "cpu": 40.0, "pad": "x" * 200}
        with open(self.path, "w") as fh:
            for _ in range(8000):
                fh.write(json.dumps(old) + "\n")
        self.h.append({"t": 1_000_000, "cpu": 41.0}, self.path, now=1_000_000)
        self.assertEqual([r["t"] for r in self.h.load(0, self.path)], [1_000_000])

    def test_one_peak_is_only_info_and_a_long_hot_period_is_a_warning(self):
        peak = self.h.summarize(self._rows([60] * 30 + [97] + [60] * 30))
        self.assertEqual(self.h.verdict(peak), "info")
        long_hot = self.h.summarize(self._rows([92] * 50 + [60] * 10))
        self.assertEqual(long_hot["hotMinutes"], 50)
        self.assertEqual(self.h.verdict(long_hot), "warning")

    def test_extreme_temperature_for_minutes_is_critical(self):
        s = self.h.summarize(self._rows([101] * 6 + [60] * 10))
        self.assertEqual(self.h.verdict(s), "critical")

    def test_cool_history_gives_no_verdict_and_empty_gives_nothing(self):
        self.assertEqual(self.h.verdict(self.h.summarize(self._rows([55] * 100))), "")
        self.assertEqual(self.h.summarize([]), {})
        self.assertEqual(self.h.verdict({}), "")

    def test_series_averages_each_bucket(self):
        rows = [{"t": 0, "cpu": 40, "sysW": 2}, {"t": 60, "cpu": 60, "sysW": 4}, {"t": 400, "cpu": 50}]
        pts = self.h.series(rows, bucket=300)
        self.assertEqual(pts[0], {"t": 0, "cpu": 50.0, "w": 3.0})
        self.assertEqual(pts[1]["cpu"], 50.0)

    def test_the_report_uses_the_history_not_a_single_reading_for_the_score(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "core", "generate-report.sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("history.py", script)
        line = next(l for l in script.splitlines() if "Температура CPU зараз висока" in l)
        self.assertIn("add_issue info", line)


class ThermalHidTests(unittest.TestCase):
    def setUp(self):
        import thermal_hid
        self.mod = thermal_hid

    def test_summary_takes_die_nand_and_battery_sensors(self):
        r = [("PMU tdie1", 50.0), ("PMU2 tdie1", 60.0), ("PMU tdev1", -22.0), ("NAND CH0 temp", 48.0),
             ("gas gauge battery", 35.0), ("gas gauge battery", 37.0)]
        self.assertEqual(self.mod.summarize(r), {"cpu": 55.0, "ssd": 48.0, "battery": 36.0})

    def test_nonsense_values_are_dropped_and_missing_sensors_are_absent(self):
        self.assertEqual(self.mod.summarize([("PMU tdie1", -22.0), ("PMU tdie2", 500.0)]), {})
        self.assertEqual(self.mod.summarize([]), {})

    def test_read_never_raises_and_gives_plausible_values_when_sensors_exist(self):
        got = self.mod.read()
        if got and "cpu" in got:
            self.assertTrue(10 < got["cpu"] < 120)

    def test_the_report_asks_the_helper_without_root(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "core", "generate-report.sh"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("thermal_hid.py", script)
        self.assertNotIn("sudo powermetrics", script)


class LauncherTests(unittest.TestCase):
    """The menu bar icon must appear when the app is opened by Launch Services."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "build_app.sh"), encoding="utf-8") as fh:
            self.build = fh.read()
        start = self.build.index("<<'LAUNCHER'")
        self.launcher = self.build[start:self.build.index("\nLAUNCHER\n", start)]

    def test_python_is_a_child_not_an_exec(self):
        # exec into Python.app made the status item disappear when the app was started with `open`
        self.assertNotIn("exec ", self.launcher)
        self.assertIn('"$PY" "$HERE/Contents/Resources/agent.py" "$@" &', self.launcher)

    def test_launcher_stops_its_child_and_waits_for_it(self):
        self.assertIn("trap 'kill \"$CHILD\" 2>/dev/null' TERM INT HUP", self.launcher)
        self.assertIn('wait "$CHILD"', self.launcher)

    def test_launcher_script_is_valid_bash(self):
        body = self.launcher.split("\n", 1)[1]
        r = subprocess.run(["/bin/bash", "-n"], input=body, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class ScoreModelTests(unittest.TestCase):
    """The score: critical problems count, warnings are capped, recommendations and the daemon do not."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "core", "generate-report.sh"), encoding="utf-8") as fh:
            self.script = fh.read()
        start = self.script.index("compute_penalty() {")
        end = self.script.index("\n}\n", start) + 3
        self.func = self.script[start:end]

    def penalty(self, lines):
        r = subprocess.run(["/bin/bash", "-c", self.func + '\ncompute_penalty'], input="", capture_output=True, text=True,
                           env=dict(os.environ, PEN_LINES="".join(l + "\n" for l in lines)))
        return int(r.stdout.strip())

    def test_recommendations_cost_nothing(self):
        self.assertEqual(self.penalty(["info|Безпека"] * 12), 0)

    def test_a_critical_costs_15_and_all_criticals_stop_at_45(self):
        self.assertEqual(self.penalty(["critical|Безпека"]), 15)
        self.assertEqual(self.penalty(["critical|Безпека"] * 6), 45)

    def test_warnings_are_capped_per_category_and_in_total(self):
        self.assertEqual(self.penalty(["warning|Памʼять"]), 4)
        self.assertEqual(self.penalty(["warning|Памʼять"] * 5), 8)
        many = [f"warning|Категорія{i}" for i in range(10)]
        self.assertEqual(self.penalty(many), 20)

    def test_the_old_seven_warnings_no_longer_read_as_a_bad_mac(self):
        old_case = ["warning|Безпека", "warning|Резервні копії", "warning|Стабільність", "warning|Памʼять"] + ["warning|XBrain daemon"] * 3
        self.assertGreaterEqual(100 - self.penalty([l for l in old_case if "XBrain" not in l]), 80)

    def test_daemon_issues_do_not_enter_the_penalty(self):
        self.assertIn('[ "$cat" = "XBrain daemon" ] || PEN_LINES=', self.script)

    def test_facts_to_review_are_not_security_warnings(self):
        for kind in ("STEALTH_KIND", "EXT_KIND", "LAUNCH_KIND", "LISTEN_KIND"):
            self.assertNotRegex(self.script, kind + r'="warning"')
            self.assertNotRegex(self.script, kind + r'="critical"')
        self.assertIn('info) status="до відома"', self.script)

    def test_a_one_moment_cpu_spike_is_not_a_warning(self):
        line = next(l for l in self.script.splitlines() if "Процес навантажує CPU: ${sname}" in l)
        self.assertIn("add_issue info", line)

    def test_missing_backups_stay_a_warning_because_data_loss_is_a_real_risk(self):
        line = next(l for l in self.script.splitlines() if "Резервні копії не налаштовано" in l and "add_issue" in l)
        self.assertIn("add_issue warning", line)

    def test_recommendations_are_not_warnings(self):
        for title in ("Брандмауер macOS вимкнено", "Доступні оновлення macOS"):
            line = next(l for l in self.script.splitlines() if title in l and "add_issue" in l)
            self.assertIn("add_issue info", line, title)


class BatteryHealthSourceTests(unittest.TestCase):
    """The report takes the battery health that macOS itself shows, not the raw FullChargeCapacity ratio."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "core", "generate-report.sh"), encoding="utf-8") as fh:
            self.script = fh.read()

    def test_maximum_capacity_from_system_profiler_is_parsed(self):
        sample = "      Health Information:\n          Cycle Count: 91\n          Condition: Normal\n          Maximum Capacity: 97%\n"
        line = next(l for l in self.script.splitlines() if "Maximum Capacity" in l and "sed -nE" in l)
        expr = line.split("sed -nE '", 1)[1].split("'", 1)[0]
        r = subprocess.run(["sed", "-nE", expr], input=sample, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "97")

    def test_system_profiler_comes_before_the_raw_ratio(self):
        self.assertLess(self.script.index("macOS Maximum Capacity"), self.script.index("FullChargeCapacity / DesignCapacity"))


class ActionsTests(unittest.TestCase):
    def setUp(self):
        import actions
        self.actions = actions

    def _wait(self):
        for _ in range(100):
            if self.actions.status()["state"] != "running":
                return self.actions.status()
            time.sleep(0.02)
        self.fail("action did not finish")

    def test_every_action_is_a_fixed_argument_list_without_shell(self):
        for a in self.actions.ACTIONS:
            with self.subTest(a=a["id"]):
                self.assertIn(a["level"], self.actions.LEVELS)
                self.assertTrue(a["fn"] is not None or (isinstance(a["argv"], list) and all(isinstance(x, str) for x in a["argv"])))
                if a["argv"]:
                    self.assertNotIn(a["argv"][0], ("sh", "bash", "zsh", "/bin/sh", "/bin/bash", "/bin/zsh"))

    def test_unknown_id_and_injection_are_refused(self):
        for bad in ("rm -rf /", "../x", None, 5, "restart_finder; reboot"):
            self.assertFalse(self.actions.start(bad, True)["ok"])

    def test_mild_and_destructive_need_confirmation(self):
        ran = []
        self.actions._execute = lambda a: (ran.append(a["id"]), (0, "ok"))[1]
        for aid in ("restart_finder", "npm_cache", "empty_trash"):
            self.assertFalse(self.actions.start(aid, None)["ok"])
            self.assertFalse(self.actions.start(aid, "yes")["ok"])
        self.assertEqual(ran, [])

    def test_read_runs_without_confirmation_and_reports_output(self):
        self.actions._execute = lambda a: (0, "hello")
        self.assertTrue(self.actions.start("disk_free", None)["ok"])
        st = self._wait()
        self.assertEqual((st["state"], st["output"], st["id"]), ("done", "hello", "disk_free"))

    def test_failed_action_reports_error_state(self):
        self.actions._execute = lambda a: (1, "boom")
        self.actions.start("disk_free", None)
        self.assertEqual(self._wait()["state"], "error")

    def test_real_read_action_runs(self):
        importlib.reload(self.actions)
        self.assertTrue(self.actions.start("disk_free", None)["ok"])
        st = self._wait()
        self.assertEqual(st["state"], "done")
        self.assertIn("Filesystem", st["output"])

    def test_launchagent_fix_changes_only_own_secret_plists_to_600(self):
        home = os.path.realpath(tempfile.mkdtemp(prefix="macsafe-la-"))
        saved = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            folder = os.path.join(home, "Library", "LaunchAgents")
            os.makedirs(folder)
            secret = os.path.join(folder, "a.plist")
            plain = os.path.join(folder, "b.plist")
            target = os.path.join(home, "outside.plist")
            for path, text in ((secret, "<key>API_TOKEN</key>"), (plain, "<key>Label</key>"), (target, "SECRET")):
                with open(path, "w") as fh:
                    fh.write(text)
                os.chmod(path, 0o644)
            os.symlink(target, os.path.join(folder, "c.plist"))
            ok, text = self.actions._fix_launchagent_perms()
            self.assertTrue(ok)
            self.assertEqual(os.stat(secret).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(plain).st_mode & 0o777, 0o644)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o644)   # a symbolic link is never followed
            self.assertIn("a.plist", text)
            self.assertNotIn("a.plist", self.actions._fix_launchagent_perms()[1])   # the second run has nothing left to fix
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved
            shutil.rmtree(home, ignore_errors=True)

    def test_issue_actions_are_not_page_tiles(self):
        page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "template.html"), encoding="utf-8").read()
        self.assertNotIn("issue:", page[page.index("const ACTION_GROUPS"):page.index("const LEVEL_TEXT")])

    def test_page_list_matches_the_actions(self):
        page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "template.html"), encoding="utf-8").read()
        block = page[page.index("const ACTION_CMDS = ["):page.index("const ACTION_GROUPS")]
        ui = dict(re.findall(r"id: '([a-z_]+)'.*?cmd: (?:'([^']*)'|`([^`]*)`)", block, re.S) and
                  [(m[0], m[1] or m[2]) for m in re.findall(r"id: '([a-z_]+)'.*?cmd: (?:'([^']*)'|`([^`]*)`)", block)])
        py = {a["id"]: a["display"].replace("\'", "'") for a in self.actions.ACTIONS}
        self.assertEqual(set(ui), set(py))
        for aid, cmd in ui.items():
            self.assertEqual(cmd.replace("\'", "'"), py[aid], aid)


class LlmTests(unittest.TestCase):
    """llm reads HOME on import, so the module is reloaded with a temporary home."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="macsafe-llm-"))
        self.saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        man = os.path.join(self.home, ".ollama", "models", "manifests", "registry.ollama.ai", "library", "tiny")
        os.makedirs(man)
        with open(os.path.join(man, "latest"), "w") as fh:
            json.dump({"config": {"size": 1024 * 1024}, "layers": [{"size": 3 * 1024 * 1024}]}, fh)
        hf = os.path.join(self.home, ".cache", "huggingface", "hub", "models--org--embed", "blobs")
        os.makedirs(hf)
        with open(os.path.join(hf, "abc"), "wb") as fh:
            fh.write(b"x" * (2 * 1024 * 1024))
        os.makedirs(os.path.join(self.home, ".cache", "huggingface", "hub", "datasets--org--data"))
        lm = os.path.join(self.home, ".lmstudio", "models", "pub", "model")
        os.makedirs(lm)
        with open(os.path.join(lm, "m.gguf"), "wb") as fh:
            fh.write(b"x" * (1024 * 1024))
        import llm
        self.llm = importlib.reload(llm)
        self.llm._running_names = lambda: set()

    def tearDown(self):
        if self.saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.saved_home
        shutil.rmtree(self.home, ignore_errors=True)
        importlib.reload(self.llm)

    def test_scan_finds_models_of_all_tools(self):
        found = {i["id"]: i for i in self.llm.scan()}
        self.assertEqual(set(found), {"ollama:tiny:latest", "huggingface:org/embed", "lmstudio:pub/model"})
        self.assertAlmostEqual(found["ollama:tiny:latest"]["sizeMb"], 4.0, delta=0.1)
        self.assertAlmostEqual(found["huggingface:org/embed"]["sizeMb"], 2.0, delta=0.1)

    def test_remove_refuses_unknown_ids_and_empty_list(self):
        self.assertFalse(self.llm.remove([])["ok"])
        r = self.llm.remove(["../../etc", "huggingface:nope"])["results"]
        self.assertTrue(all(not x["ok"] for x in r))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".cache", "huggingface", "hub", "models--org--embed")))

    def test_ollama_model_is_removed_through_the_command_only(self):
        calls = []
        self.llm._ollama_bin = lambda: "/bin/ollama"
        fake = lambda cmd, **kw: (calls.append(cmd), type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())[1]
        with unittest.mock.patch.object(self.llm.subprocess, "run", fake):
            r = self.llm.remove(["ollama:tiny:latest"])
        self.assertEqual(calls, [["/bin/ollama", "rm", "tiny:latest"]])
        self.assertTrue(r["results"][0]["ok"])

    def test_blocked_tool_is_skipped(self):
        self.llm._running_names = lambda: {"lm studio helper"}
        item = next(i for i in self.llm.scan() if i["tool"] == "lmstudio")
        self.assertTrue(item["blocked"])
        r = self.llm.remove(["lmstudio:pub/model"])["results"][0]
        self.assertTrue(r.get("skipped"))
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".lmstudio", "models", "pub", "model", "m.gguf")))

    def test_models_are_never_in_the_cache_cleaning(self):
        import cleanup
        self.assertTrue(any(w in "llama.cpp" for w in cleanup.PROTECTED_WORDS))


class OldCacheCleanTests(unittest.TestCase):
    """Quick clean: only files older than the threshold; recent files, symbolic links and the root stay."""

    def setUp(self):
        import cleanup
        self.cleanup = cleanup
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "cache")
        os.makedirs(os.path.join(self.root, "sub", "empty"))
        self.old = os.path.join(self.root, "sub", "old.bin")
        self.new = os.path.join(self.root, "fresh.bin")
        for path, age_days in ((self.old, 90), (self.new, 1)):
            with open(path, "wb") as fh:
                fh.write(b"x" * 1024 * 1024)
            stamp = time.time() - age_days * 86400
            os.utime(path, (stamp, stamp))
        self.link = os.path.join(self.root, "link")
        os.symlink(self.old, self.link)
        self._orig_target = cleanup._target
        cleanup._target = lambda cid: self.root
        self._orig_running = cleanup.running_apps
        cleanup.running_apps = lambda: (set(), set())   # the real running apps must not decide the test

    def tearDown(self):
        self.cleanup._target = self._orig_target
        self.cleanup.running_apps = self._orig_running
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_old_files_removed(self):
        result = self.cleanup.clean_old(["npm"], days=30)
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(self.old))
        self.assertTrue(os.path.exists(self.new))
        self.assertTrue(os.path.islink(self.link))
        self.assertTrue(os.path.isdir(self.root))
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub", "empty")))
        self.assertGreater(result["freedMb"], 0.9)

    def test_size_matches_what_gets_removed(self):
        cutoff = time.time() - 30 * 86400
        found = self.cleanup._walk_old(self.root, cutoff)
        self.assertEqual([path for path, _ in found], [self.old])


class CategoryCleanTests(OldCacheCleanTests):
    """Detailed cleaning: categories, age threshold, memory snapshot; logs are now in the allowed roots."""

    def test_unselected_category_is_skipped(self):
        result = self.cleanup.clean_old(["npm"], days=30, categories=["logs"])
        self.assertTrue(os.path.exists(self.old))
        self.assertIn("error", result["results"][0])

    def test_selected_category_cleans(self):
        result = self.cleanup.clean_old(["npm"], days=30, categories=["dev"])
        self.assertFalse(os.path.exists(self.old))
        self.assertGreater(result["results"][0]["freedMb"], 0.9)

    def test_invalid_days_falls_back_to_default(self):
        result = self.cleanup.clean_old(["npm"], days=1, categories=None)   # 1 day, not allowed
        self.assertEqual(result["days"], self.cleanup.OLD_DAYS)
        self.assertTrue(os.path.exists(self.new))

    def test_result_has_memory_block(self):
        result = self.cleanup.clean_old(["npm"])
        self.assertIn("memory", result)
        self.assertEqual(set(result["memory"]), {"before", "after", "delta"})

    def test_categories_of_ids(self):
        c = self.cleanup
        self.assertEqual(c.category_of("npm"), "dev")
        self.assertEqual(c.category_of("logs"), "logs")
        self.assertEqual(c.category_of("cache:Foo"), "apps")
        self.assertIsNone(c.category_of("../etc"))

    def test_memory_delta(self):
        a = {"freeMb": 10, "availableMb": 100, "inactiveMb": 5, "swapMb": 0}
        b = {"freeMb": 30, "availableMb": 90, "inactiveMb": 1, "swapMb": 2}
        self.assertEqual(self.cleanup.memory_delta(a, b),
                         {"freeMb": 20, "availableMb": -10, "inactiveMb": -4, "swapMb": 2})
        self.assertIsNone(self.cleanup.memory_delta(None, b))


class LogsRootTests(unittest.TestCase):
    def test_logs_root_allowed(self):
        import cleanup
        logs = os.path.join(cleanup.HOME, "Library", "Logs")
        self.assertIn(logs, cleanup.ALLOWED_ROOTS)


class KillableTests(unittest.TestCase):
    """“×” is available for any user process except MacSafe itself."""

    def test_user_process_is_killable(self):
        import metrics
        self.assertTrue(metrics.killable("Ghostty", [101]))
        self.assertTrue(metrics.killable("claude", [202, 203]))
        self.assertTrue(metrics.killable("Finder", [303]))

    def test_group_without_own_processes_is_not_killable(self):
        import metrics
        self.assertFalse(metrics.killable("WindowServer", []))

    def test_self_is_not_killable(self):
        import metrics
        self.assertFalse(metrics.killable(metrics.SELF_NAME, [404]))



class KillMarkTests(unittest.TestCase):
    """“Sure?” is on one row by the running index: CPU rows, then RAM rows."""

    class _Button:
        def __init__(self):
            self.title = "×"

        def setTitle_(self, title):
            self.title = title

    def test_armed_title_across_both_lists(self):
        import menubar
        cpu = [(None, None, None, None, self._Button(), None, None) for _ in range(2)]
        ram = [(None, None, None, None, self._Button(), None, None) for _ in range(2)]

        class Panel:
            process_rows = [cpu, ram]
        bar = menubar.StatusBar.__new__(menubar.StatusBar)
        bar._panel = Panel()
        bar._mark_kill(2)   # the first RAM row
        self.assertEqual([r[4].title for r in cpu + ram], ["×", "×", "Так?", "×"])
        bar._mark_kill(None)
        self.assertEqual([r[4].title for r in cpu + ram], ["×", "×", "×", "×"])


class PinnedRowsTests(unittest.TestCase):
    """Pinned rows: the order stays, values come from a fresh snapshot, vanished ones are marked."""

    def test_stable_names_keep_positions_and_fill_free_slots(self):
        import menubar
        top = [{"name": "a"}, {"name": "c"}, {"name": "b"}]
        # b has dropped out of the top 2: c takes its place, a stays in place
        self.assertEqual(menubar._stable_names(["a", "b"], top, 2), ["a", "c"])
        # the order of already known rows does not change, even if the CPU order is now different
        self.assertEqual(menubar._stable_names(["b", "a"], [{"name": "a"}, {"name": "b"}], 2), ["b", "a"])
        # at first the list is empty: new ones take places in CPU order, extra slots — None
        self.assertEqual(menubar._stable_names([], [{"name": "x"}, {"name": "y"}], 3), ["x", "y", None])

    def test_pinned_keeps_position_and_takes_fresh_values(self):
        import menubar
        pinned = [{"name": "a", "cpu": 1.0}, None, {"name": "b", "cpu": 2.0}]
        live = {"b": {"name": "b", "cpu": 9.0}, "a": {"name": "a", "cpu": 5.0}}
        out = menubar._pinned_groups(pinned, live)
        self.assertEqual([(g and g["cpu"], stale) for g, stale in out], [(5.0, False), (None, False), (9.0, False)])

    def test_pinned_marks_vanished_process_stale(self):
        import menubar
        pinned = [{"name": "gone", "cpu": 3.0}]
        out = menubar._pinned_groups(pinned, {})
        self.assertEqual(out[0][0]["cpu"], 3.0)   # the last known values
        self.assertTrue(out[0][1])                 # marked as gone


class AlertsTests(unittest.TestCase):
    def setUp(self):
        import alerts
        self.alerts = alerts
        self.now = [0.0]
        self.rules = alerts.Alerts(clock=lambda: self.now[0])

    def snap(self, ram=50, free=100, bat=None):
        return {"ram": {"percent": ram}, "disk": {"freeGb": free}, "battery": bat}

    def test_ram_needs_hold_then_cooldown(self):
        self.assertEqual(self.rules.check(self.snap(ram=95)), [])
        self.now[0] = 61
        self.assertEqual(len(self.rules.check(self.snap(ram=95))), 1)
        self.now[0] = 200
        self.assertEqual(self.rules.check(self.snap(ram=95)), [])      # pause
        self.now[0] = 4000
        self.assertEqual(len(self.rules.check(self.snap(ram=95))), 1)  # the pause has passed

    def test_ram_recovery_resets_hold(self):
        self.rules.check(self.snap(ram=95))
        self.now[0] = 30
        self.rules.check(self.snap(ram=50))
        self.now[0] = 70
        self.assertEqual(self.rules.check(self.snap(ram=95)), [])

    def test_disk_and_battery_fire_at_once(self):
        out = self.rules.check(self.snap(free=3, bat={"percent": 10.0, "plugged": False}))
        self.assertEqual(len(out), 2)

    def test_charging_battery_silent(self):
        self.assertEqual(self.rules.check(self.snap(bat={"percent": 5.0, "plugged": True})), [])

    def test_security_increase_only(self):
        self.assertEqual(self.rules.check(self.snap(), {"level": "warning", "issues": 2}), [])
        self.assertEqual(self.rules.check(self.snap(), {"level": "warning", "issues": 2}), [])
        self.assertEqual(len(self.rules.check(self.snap(), {"level": "warning", "issues": 3})), 1)


class NewPrefsTests(PrefsTests):
    def test_flags_roundtrip(self):
        from prefs import Prefs
        p = Prefs(self.path)
        self.assertTrue(p.get("alerts"))
        p.set("dailyCheck", False)
        self.assertFalse(Prefs(self.path).get("dailyCheck"))


class SystemMetricsTests(unittest.TestCase):
    def test_disk_and_battery_shapes(self):
        import metrics
        d = metrics.disk_info()
        self.assertTrue(d is None or d["totalGb"] > 0)
        b = metrics.battery_info()
        self.assertTrue(b is None or 0 <= b["percent"] <= 100)

    def test_net_rates_first_call_none(self):
        import metrics
        metrics._NET.update(at=None, rx=None, tx=None)
        self.assertIsNone(metrics.net_rates(1.0))
        r = metrics.net_rates(2.0)
        self.assertTrue(r is None or (r[0] >= 0 and r[1] >= 0))


class BigFilesTests(unittest.TestCase):
    def setUp(self):
        import bigfiles
        self.b = bigfiles
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(self.tmp, "Library"))
        os.makedirs(os.path.join(self.tmp, "Movies"))
        os.makedirs(os.path.join(self.tmp, ".hidden"))
        self.big = os.path.join(self.tmp, "Movies", "big.mov")
        self.mid = os.path.join(self.tmp, "mid.bin")
        for path, mb in ((self.big, 60), (self.mid, 51), (os.path.join(self.tmp, "small.txt"), 1),
                         (os.path.join(self.tmp, "Library", "skipped.bin"), 60),
                         (os.path.join(self.tmp, ".hidden", "skipped.bin"), 60)):
            with open(path, "wb") as fh:
                fh.truncate(mb * 1024 * 1024)
        os.symlink(self.big, os.path.join(self.tmp, "link.mov"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_sorted_and_skips_service_dirs_and_links(self):
        r = self.b.find_big(50, root=self.tmp)
        self.assertEqual([f["path"] for f in r["files"]], [self.big, self.mid])
        self.assertFalse(r["truncated"])

    def test_limit(self):
        self.assertEqual(len(self.b.find_big(50, limit=1, root=self.tmp)["files"]), 1)

    def test_invalid_min_falls_back(self):
        self.assertEqual(self.b.find_big(7, root=self.tmp)["minMb"], 100)

    def test_safe_path(self):
        orig = self.b.HOME
        self.b.HOME = self.tmp
        try:
            self.assertEqual(self.b.safe_path(self.big), self.big)
            self.assertIsNone(self.b.safe_path(os.path.join(self.tmp, "link.mov")))
            self.assertIsNone(self.b.safe_path("/etc/hosts"))
            self.assertIsNone(self.b.safe_path("relative/path"))
            self.assertIsNone(self.b.safe_path(os.path.join(self.tmp, "..", "x")))
        finally:
            self.b.HOME = orig


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        import persistence
        self.p = persistence
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.dir)
        self.state = os.path.join(self.tmp, "state", "s.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, label):
        import plistlib
        with open(os.path.join(self.dir, name), "wb") as fh:
            plistlib.dump({"Label": label, "ProgramArguments": ["/bin/echo", "x"]}, fh)

    def test_first_run_is_silent_then_new_item_reported(self):
        self.write("a.plist", "com.a")
        self.assertEqual(self.p.check(self.state, (self.dir,)), [])
        self.assertEqual(self.p.check(self.state, (self.dir,)), [])
        self.write("b.plist", "com.b")
        out = self.p.check(self.state, (self.dir,))
        self.assertEqual([i["label"] for i in out], ["com.b"])
        self.assertEqual(out[0]["program"], "/bin/echo")
        self.assertEqual(self.p.check(self.state, (self.dir,)), [])

    def test_broken_plist_does_not_break_scan(self):
        with open(os.path.join(self.dir, "bad.plist"), "wb") as fh:
            fh.write(b"not a plist")
        found = self.p.scan((self.dir,))
        self.assertEqual(list(found.values())[0]["label"], "bad")


class LeftoversTests(unittest.TestCase):
    def setUp(self):
        import leftovers, plistlib
        self.l = leftovers
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.apps = os.path.join(self.tmp, "Apps")
        self.support = os.path.join(self.tmp, "Support")
        app = os.path.join(self.apps, "Kept.app", "Contents")
        os.makedirs(app)
        with open(os.path.join(app, "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "com.vendor.kept"}, fh)
        broken = os.path.join(self.apps, "Broken.app", "Contents")
        os.makedirs(broken)
        with open(os.path.join(broken, "Info.plist"), "wb") as fh:
            fh.write(b"<not a plist")
        old = time.time() - 90 * 86400
        for name in ("com.vendor.kept", "com.vendor.gone", "com.apple.thing", "plain-folder", "com.vendor.fresh"):
            path = os.path.join(self.support, name)
            os.makedirs(path)
            with open(os.path.join(path, "data"), "wb") as fh:
                fh.write(b"x" * 1024)
            if name != "com.vendor.fresh":
                os.utime(path, (old, old))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_orphans_found(self):
        found = self.l.scan((self.support,), (self.apps,))
        self.assertEqual([i["id"] for i in found], ["com.vendor.gone"])

    def test_broken_info_plist_does_not_break(self):
        self.assertIn("com.vendor.kept", self.l.installed_ids((self.apps,)))


class TrashTargetTests(unittest.TestCase):
    def setUp(self):
        import trash
        self.t = trash
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.orig = trash.HOME
        trash.HOME = self.tmp

    def tearDown(self):
        self.t.HOME = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rules(self):
        f = os.path.join(self.tmp, "a.bin")
        open(f, "wb").close()
        os.makedirs(os.path.join(self.tmp, ".Trash"))
        inside_trash = os.path.join(self.tmp, ".Trash", "x")
        open(inside_trash, "wb").close()
        os.symlink(f, os.path.join(self.tmp, "ln"))
        self.assertEqual(self.t.safe_target(f), f)
        self.assertIsNone(self.t.safe_target(inside_trash))
        self.assertIsNone(self.t.safe_target(os.path.join(self.tmp, "ln")))
        self.assertIsNone(self.t.safe_target(self.tmp))
        self.assertIsNone(self.t.safe_target("/etc/hosts"))
        self.assertEqual(self.t.move_to_trash("/etc/hosts"), (False, "Недозволений шлях"))


if __name__ == "__main__":
    unittest.main()
