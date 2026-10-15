"""Behavior and API checks; all records are isolated in temporary directories."""
import copy
from contextlib import closing
import datetime as dt
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from paperdesk import Store, ValidationError, make_server


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperdesk-test-")
        self.day = dt.date(2026, 10, 14)
        self.store = Store(Path(self.temp.name), lambda: self.day)
        self.journal = self.store.journal_save({"name": "测试期刊", "url": "https://example.com/submit"})
        self.next_journal = self.store.journal_save({"name": "候选期刊", "url": ""})
        self.paper = self.store.paper_save({"title": "测试论文", "stage": "writing", "authors": "测试作者", "tags": "AI"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submission(self, **overrides):
        data = {"journal_id": self.journal, "submitted_on": "2026-10-14", "transfer_mode": "none", "candidate_ids": [self.next_journal]}
        data.update(overrides)
        return data

    def current(self):
        return self.store.paper_detail(self.paper)["submissions"][0]

    def test_persistence_and_day_zero(self):
        self.store.submission_save(self.paper, self.submission())
        self.assertEqual(self.current()["elapsed_days"], 0)
        self.store.close()
        self.store = Store(Path(self.temp.name), lambda: self.day)
        self.assertEqual(self.store.paper_detail(self.paper)["title"], "测试论文")
        self.assertEqual(self.current()["candidates"][0]["id"], self.next_journal)

    def test_due_today_not_overdue_next_day_is(self):
        data = self.submission(submitted_on="2026-09-14", transfer_mode="days", wait_days=30)
        ident = self.store.submission_save(self.paper, data)
        self.assertEqual(self.current()["target_on"], "2026-10-14")
        self.assertEqual(self.current()["overdue_days"], 0)
        self.day += dt.timedelta(days=1)
        self.assertEqual(self.current()["overdue_days"], 1)
        self.assertEqual(self.store.state()["overdue_count"], 1)
        self.assertEqual(self.store.paper_detail(self.paper)["stage"], "submitted")
        self.store.submission_save(self.paper, {**data, "transfer_mode": "none"}, ident)
        self.assertEqual(self.current()["overdue_days"], 0)

    def test_leap_day_year_boundary_and_start_edit(self):
        self.day = dt.date(2024, 3, 1)
        ident = self.store.submission_save(self.paper, self.submission(submitted_on="2024-02-28", transfer_mode="days", wait_days=2))
        self.assertEqual(self.current()["elapsed_days"], 2)
        self.assertEqual(self.current()["target_on"], "2024-03-01")
        self.day = dt.date(2025, 1, 1)
        self.store.submission_save(self.paper, self.submission(submitted_on="2024-12-31", transfer_mode="days", wait_days=2), ident)
        self.assertEqual(self.current()["elapsed_days"], 1)
        self.assertEqual(self.current()["target_on"], "2025-01-02")

    def test_full_history_retention_rejection_transfer_acceptance(self):
        first = self.submission(submitted_on="2026-09-01", transfer_mode="date", target_on="2026-10-01")
        ident = self.store.submission_save(self.paper, first)
        self.store.paper_save({**self.store.paper_detail(self.paper), "stage": "revision"}, self.paper)
        self.assertGreater(self.current()["overdue_days"], 0)
        self.store.submission_save(self.paper, {**first, "ended_on": "2026-10-10", "result": "rejected"}, ident)
        self.assertEqual(self.store.paper_detail(self.paper)["stage"], "writing")
        self.assertEqual(self.current()["elapsed_days"], 39)
        self.day = dt.date(2026, 10, 20)
        self.assertEqual(self.current()["elapsed_days"], 39)
        second = self.submission(journal_id=self.next_journal, submitted_on="2026-10-14", candidate_ids=[])
        second_id = self.store.submission_save(self.paper, second)
        self.assertEqual(len(self.store.paper_detail(self.paper)["submissions"]), 2)
        self.store.submission_save(self.paper, {**second, "ended_on": "2026-10-20", "result": "accepted"}, second_id)
        self.assertEqual(self.store.paper_detail(self.paper)["stage"], "accepted")
        self.assertEqual(self.store.state()["overdue_count"], 0)

    def test_single_active_and_transaction_rollback(self):
        self.store.submission_save(self.paper, self.submission())
        before = self.store.export()["data"]
        with self.assertRaises(ValidationError):
            self.store.submission_save(self.paper, self.submission())
        self.assertEqual(self.store.export()["data"], before)
        with self.assertRaises(ValidationError):
            self.store.paper_save({"title": "不能覆盖", "stage": "writing"}, self.paper)
        self.assertEqual(self.store.paper_detail(self.paper)["title"], "测试论文")

    def test_concurrent_submissions_only_one_succeeds(self):
        outcomes = []
        def save():
            try:
                self.store.submission_save(self.paper, self.submission())
                outcomes.append("saved")
            except ValidationError:
                outcomes.append("rejected")
        threads = [threading.Thread(target=save) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["saved", "rejected"])

    def test_archive_suppresses_overdue_unarchive_restores(self):
        self.store.submission_save(self.paper, self.submission(submitted_on="2026-09-01", transfer_mode="date", target_on="2026-09-30"))
        paper = self.store.paper_detail(self.paper)
        self.store.paper_save({**paper, "stage": "archived"}, self.paper)
        self.assertEqual(self.store.state()["overdue_count"], 0)
        self.store.paper_save({**paper, "stage": "submitted"}, self.paper)
        self.assertEqual(self.store.state()["overdue_count"], 1)

    def test_invalid_dates_plan_and_links(self):
        cases = [dict(submitted_on="2026-10-15"), dict(submitted_on="2026-02-30"),
                 dict(transfer_mode="days", wait_days=0), dict(transfer_mode="days", wait_days=True),
                 dict(transfer_mode="date", target_on="2026-10-01"),
                 dict(ended_on="2026-10-13", result="rejected"), dict(ended_on="2026-10-14"),
                 dict(candidate_ids=[self.next_journal, self.next_journal])]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.store.submission_save(self.paper, self.submission(**changes))
        for url in ("javascript:alert(1)", "https://", "https://user:password@example.com", "file:///C:/test.pdf"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.store.journal_save({"name": "非法链接", "url": url})
        self.assertEqual(len(self.store.paper_detail(self.paper)["submissions"]), 0)

    def test_export_restore_all_tables_and_bad_import_unchanged(self):
        document = Path(self.temp.name) / "稿件.docx"
        document.write_text("test", encoding="utf-8")
        self.store.paper_save({"title": "测试论文", "stage": "writing", "folder": str(Path(self.temp.name)), "files": [{"label": "投稿稿", "path": str(document)}]}, self.paper)
        self.store.submission_save(self.paper, self.submission(submitted_on="2026-09-01", transfer_mode="days", wait_days=60, progress="外审中"))
        exported = self.store.export()
        self.store.paper_delete(self.paper)
        self.store.restore(exported)
        self.assertEqual(self.store.export()["data"], exported["data"])
        before = self.store.export()["data"]
        mutations = [lambda p:p.update(schema_version=999),
                     lambda p:p["data"]["submissions"][0].update(journal_id=999),
                     lambda p:p["data"]["submissions"][0].update(paper_id=[]),
                     lambda p:p["data"]["submissions"][0].update(target_on="2020-01-01"),
                     lambda p:p["data"]["papers"][0].update(stage="writing"),
                     lambda p:p["data"]["journals"].append(p["data"]["journals"][0].copy())]
        for mutate in mutations:
            corrupt = copy.deepcopy(exported)
            mutate(corrupt)
            with self.assertRaises(ValidationError):
                self.store.restore(corrupt)
            self.assertEqual(self.store.export()["data"], before)

    def test_automatic_snapshot_integrity_retention_and_restore(self):
        backups = self.store.backups()
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(self.store.backup_dir / backups[0]["name"])) as saved:
            self.assertEqual(saved.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        exported = self.store.export()
        self.store.snapshot("restore")
        snapshot = self.store.backups()[0]["name"]
        self.store.paper_delete(self.paper)
        self.store.restore_snapshot(snapshot)
        self.assertEqual(self.store.export()["data"], exported["data"])
        self.day += dt.timedelta(days=31)
        self.store.journal_save({"name": "新的一天"})
        self.assertEqual(len(self.store.backups()), 1)
        with self.assertRaises(ValidationError):
            self.store.restore_snapshot("../../paperdesk.sqlite3")

    def test_backup_and_restore_write_failure_cannot_overwrite_records(self):
        before = self.store.export()
        with patch.object(self.store, "snapshot", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.paper_delete(self.paper)
        self.assertEqual(self.store.export()["data"], before["data"])

        original = Store.insert_export
        calls = 0
        def failed_live_write(connection, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.IntegrityError("simulated restore write failure")
            return original(connection, data)
        with patch.object(Store, "insert_export", side_effect=failed_live_write):
            with self.assertRaises(ValidationError):
                self.store.restore(before)
        self.assertEqual(self.store.export()["data"], before["data"])

    def test_original_v1_backup_still_restores_without_app_version(self):
        payload = json.loads((Path(__file__).parent / "testdata" / "v1-backup.json").read_text(encoding="utf-8"))
        original = copy.deepcopy(payload)
        self.store.restore(payload)
        restored = self.store.export()["data"]
        expected = copy.deepcopy(payload["data"])
        for paper in expected["papers"]:
            paper.update(planned_on=None, writing_started_on=None, target_journal_id=None)
        self.assertEqual(restored, expected)
        self.assertEqual(payload, original)

    def test_original_database_upgrade_and_old_snapshot_restore(self):
        fixture = Path(__file__).parent / "testdata"
        data = json.loads((fixture / "v1-backup.json").read_text(encoding="utf-8"))["data"]
        self.store.close()
        Path(self.temp.name, "paperdesk.sqlite3").unlink()
        with closing(sqlite3.connect(Path(self.temp.name, "paperdesk.sqlite3"))) as old:
            old.executescript((fixture / "v1-schema.sql").read_text(encoding="utf-8"))
            Store.insert_export(old, data)
            old.commit()
        self.store = Store(Path(self.temp.name), lambda: self.day)
        self.assertEqual(self.store.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        for paper in self.store.export()["data"]["papers"]:
            paper.pop("planned_on")
            paper.pop("writing_started_on")
            paper.pop("target_journal_id")
            self.assertIn(paper, data["papers"])
        snapshot = next(b["name"] for b in self.store.backups() if b["name"].startswith("upgrade-"))
        with closing(sqlite3.connect(self.store.backup_dir / snapshot)) as saved:
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 1)
        self.store.paper_delete(data["papers"][0]["id"])
        self.store.restore_snapshot(snapshot)
        restored = self.store.paper_detail(data["papers"][0]["id"])
        self.assertIsNone(restored["planned_on"])
        self.assertIsNone(restored["target_journal_id"])
        self.assertEqual(restored["submissions"][0]["candidates"][0]["name"], "原版候选期刊")

    def test_initial_target_survives_submission_transfer_and_restore(self):
        paper = self.store.paper_save({"title": "目标独立", "stage": "planned", "target_journal_id": self.journal})
        self.assertEqual(self.store.state()["papers"][0]["target_journal"]["id"], self.journal)
        with self.assertRaisesRegex(ValidationError, "初始目标"):
            self.store.journal_delete(self.journal)
        first = self.submission(journal_id=self.next_journal, candidate_ids=[self.journal])
        ident = self.store.submission_save(paper, first)
        self.assertEqual(self.store.paper_detail(paper)["target_journal_id"], self.journal)
        self.store.submission_save(paper, {**first, "ended_on": self.day.isoformat(), "result": "rejected"}, ident)
        self.store.submission_save(paper, self.submission(candidate_ids=[self.next_journal]))
        payload = self.store.export()
        self.store.restore(payload)
        self.assertEqual(self.store.paper_detail(paper)["target_journal_id"], self.journal)
        invalid = copy.deepcopy(payload)
        invalid["data"]["papers"][0]["target_journal_id"] = 999
        with self.assertRaises(ValidationError):
            self.store.restore(invalid)
        self.assertEqual(self.store.export()["data"], payload["data"])

    def test_planning_and_writing_clocks_and_optional_old_dates(self):
        self.day = dt.date(2024, 2, 28)
        paper = self.store.paper_save({"title": "阶段计时", "stage": "planned"})
        self.assertEqual(self.store.paper_detail(paper)["stage_elapsed_days"], 0)
        self.day = dt.date(2024, 3, 1)
        self.assertEqual(self.store.paper_detail(paper)["stage_elapsed_days"], 2)
        self.store.paper_save({"title": "阶段计时", "stage": "writing"}, paper)
        detail = self.store.paper_detail(paper)
        self.assertEqual(detail["planned_on"], "2024-02-28")
        self.assertEqual(detail["writing_started_on"], "2024-03-01")
        self.assertEqual(detail["stage_elapsed_days"], 0)
        self.day = dt.date(2025, 1, 1)
        self.store.paper_save({"title": "阶段计时", "stage": "writing", "writing_started_on": "2024-12-31"}, paper)
        self.assertEqual(self.store.paper_detail(paper)["stage_elapsed_days"], 1)
        self.store.paper_save({"title": "阶段计时", "stage": "writing", "writing_started_on": None}, paper)
        self.assertIsNone(self.store.paper_detail(paper)["stage_elapsed_days"])
        for fields in ({"planned_on": "2025-01-02"}, {"writing_started_on": "2024-02-30"}, {"writing_started_on": "2024-02-27"}, {"target_journal_id": 999}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                self.store.paper_save({"title": "阶段计时", "stage": "writing", **fields}, paper)

    def test_registered_file_open_and_missing_file(self):
        with self.assertRaises(ValidationError):
            self.store.paper_save({"title": "不执行程序", "files": [{"path": str(Path(self.temp.name) / "run.exe")}]})
        document = Path(self.temp.name) / "稿件.pdf"
        document.write_text("test", encoding="utf-8")
        self.store.paper_save({"title": "测试论文", "folder": self.temp.name, "files": [{"path": str(document), "label": "投稿稿"}]}, self.paper)
        file_id = self.store.paper_detail(self.paper)["files"][0]["id"]
        with patch("os.startfile", create=True) as opener:
            self.store.open_registered("file", file_id)
            opener.assert_called_once_with(str(document))
        document.unlink()
        with self.assertRaisesRegex(ValidationError, "重新关联"):
            self.store.open_registered("file", file_id)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperdesk-api-")
        self.store = Store(Path(self.temp.name))
        self.server = make_server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        req = Request(self.url + path, method=method,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers=headers or {})
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            return response.status, body, response.headers

    def test_frontend_served_and_local_api_roundtrip(self):
        code, body, headers = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("论文管理台", body.decode())
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        code, body, _ = self.request("/api/state")
        token = json.loads(body)["token"]
        headers = {"Content-Type": "application/json", "X-PaperDesk-Token": token, "Origin": self.url}
        code, body, _ = self.request("/api/papers", "POST", {"title": "接口测试", "stage": "writing"}, headers)
        self.assertEqual(code, 200)
        ident = json.loads(body)["id"]
        code, body, _ = self.request(f"/api/papers/{ident}")
        self.assertEqual(json.loads(body)["title"], "接口测试")
        code, body, _ = self.request("/api/restore", "POST", {"broken": True}, headers)
        self.assertEqual(code, 400)
        self.assertEqual(self.store.paper_detail(ident)["title"], "接口测试")

    def test_cross_site_host_and_token_rejected(self):
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request("/api/papers", "POST", {"title": "禁止"}, headers)[0], 403)
        headers["X-PaperDesk-Token"] = self.server.token
        headers["Origin"] = "https://example.com"
        self.assertEqual(self.request("/api/papers", "POST", {"title": "禁止"}, headers)[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/../paperdesk.py")[0], 404)
        self.assertEqual(self.store.state()["papers"], [])

    def test_port_is_exclusive(self):
        with self.assertRaises(OSError):
            make_server(self.store, self.server.server_port)


if __name__ == "__main__":
    unittest.main(verbosity=2)
