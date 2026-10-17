"""Behavior and API checks; all records are isolated in temporary directories."""
import copy
from contextlib import closing
import datetime as dt
import json
import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from paperdesk import Store, ValidationError, make_server, SCHEMA_VERSION, APP_VERSION, ConflictError, InvalidDatabase, UnsupportedSchema, recover_database, default_data_dir, legacy_data_dirs, prepare_default_storage, existing_url, storage_id


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

    def test_directly_add_submitted_and_revision_papers(self):
        for stage in ("submitted", "revision"):
            ident = self.store.paper_save({"title": "之前已投稿", "stage": stage,
                "target_journal_id": self.next_journal,
                "initial_submission": self.submission(submitted_on="2026-09-01", manuscript_number="OLD-001", progress="外审中", transfer_mode="days", wait_days=60)})
            detail = self.store.paper_detail(ident)
            self.assertEqual(detail["stage"], stage)
            self.assertEqual(len(detail["submissions"]), 1)
            self.assertEqual(detail["submissions"][0]["elapsed_days"], 43)
            self.assertEqual(detail["submissions"][0]["manuscript_number"], "OLD-001")
            self.assertEqual(detail["submissions"][0]["target_on"], "2026-10-31")
            self.assertEqual(detail["target_journal_id"], self.next_journal)
            self.assertIsNone(detail["writing_started_on"])
        exported = self.store.export()
        self.store.close()
        self.store = Store(Path(self.temp.name), lambda: self.day)
        self.assertEqual(self.store.export()["data"], exported["data"])

    def test_invalid_initial_submission_leaves_no_partial_paper(self):
        before = self.store.export()["data"]
        cases = [None, [], self.submission(journal_id=999), self.submission(submitted_on="2026-02-30"),
                 self.submission(submitted_on="2026-10-15"), self.submission(candidate_ids=[999]),
                 self.submission(ended_on="2026-10-14", result="rejected")]
        for initial in cases:
            with self.subTest(initial=initial), self.assertRaises(ValidationError):
                self.store.paper_save({"title": "不能只存下论文", "stage": "submitted", "initial_submission": initial})
            self.assertEqual(self.store.export()["data"], before)
        with self.assertRaises(ValidationError):
            self.store.paper_save({"title": "不能覆盖", "stage": "writing", "initial_submission": self.submission()}, self.paper)
        self.assertEqual(self.store.export()["data"], before)

    def test_initial_round_write_failure_rolls_back_paper_files_and_round(self):
        before = self.store.export()["data"]
        self.store.conn.execute("CREATE TEMP TRIGGER fail_candidate BEFORE INSERT ON candidates BEGIN SELECT RAISE(ABORT, 'simulated full disk'); END")
        try:
            with self.assertRaises(ValidationError):
                self.store.paper_save({"title": "一起回滚", "stage": "submitted", "files": [{"path": str(Path(self.temp.name) / "稿件.pdf")}], "initial_submission": self.submission()})
        finally:
            self.store.conn.execute("DROP TRIGGER fail_candidate")
        self.assertEqual(self.store.export()["data"], before)

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
                     lambda p:p["data"]["journals"][0].update(guidelines_url="javascript:alert(1)"),
                     lambda p:p["data"]["settings"][0].update(value=[]),
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
        self.assertEqual(len(backups), 2)
        with closing(sqlite3.connect(self.store.backup_dir / backups[0]["name"])) as saved:
            self.assertEqual(saved.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        exported = self.store.export()
        self.store.snapshot("restore")
        snapshot = next(b["name"] for b in self.store.backups() if b["name"].startswith("restore-"))
        self.store.paper_delete(self.paper)
        self.store.restore_snapshot(snapshot)
        self.assertEqual(self.store.export()["data"], exported["data"])
        self.day += dt.timedelta(days=31)
        self.store.journal_save({"name": "新的一天"})
        self.assertEqual(len(self.store.backups()), 2)
        with self.assertRaises(ValidationError):
            self.store.restore_snapshot("../../paperdesk.sqlite3")

    def test_restore_preview_counts_empty_guard_and_explicit_empty_restore(self):
        before = self.store.export()
        empty = next(b for b in self.store.backups() if b["counts"]["papers"] == 0)
        payload = self.store.snapshot_payload(empty["name"])
        self.assertEqual(self.store.preview_restore(payload), {"papers": 0, "journals": 0, "submissions": 0, "files": 0})
        self.assertEqual(self.store.export()["data"], before["data"])
        with self.assertRaisesRegex(ValidationError, "0 篇论文"):
            self.store.restore_snapshot(empty["name"])
        self.assertEqual(self.store.export()["data"], before["data"])
        self.store.restore_snapshot(empty["name"], allow_empty=True)
        self.assertEqual(self.store.state()["papers"], [])
        safety = next(b for b in self.store.backups() if b["name"].startswith("restore-") and b["counts"]["papers"] == 1)
        self.store.restore_snapshot(safety["name"])
        self.assertEqual(self.store.export()["data"], before["data"])

    def test_corrupt_snapshot_visible_but_preview_does_not_modify_records(self):
        bad = self.store.backup_dir / "daily-2026-10-13.sqlite3"
        bad.write_bytes(b"not a database")
        before = self.store.export()["data"]
        self.assertIsNone(next(b for b in self.store.backups() if b["name"] == bad.name)["counts"])
        with self.assertRaises(ValidationError):
            self.store.preview_restore(self.store.snapshot_payload(bad.name))
        self.assertEqual(self.store.export()["data"], before)

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
        expected["settings"] = []
        for journal in expected["journals"]:
            journal["guidelines_url"] = ""
        for paper in expected["papers"]:
            paper.update(planned_on=None, writing_started_on=None, target_journal_id=None, paper_code=f"{paper["id"]:04d}")
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
        self.assertEqual(self.store.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        for paper in self.store.export()["data"]["papers"]:
            paper.pop("planned_on")
            paper.pop("writing_started_on")
            paper.pop("target_journal_id")
            paper.pop("paper_code")
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

    def test_v2_database_json_and_snapshot_upgrade(self):
        fixture = Path(__file__).parent / "testdata"
        data = json.loads((fixture / "v1-backup.json").read_text(encoding="utf-8"))["data"]
        for paper in data["papers"]:
            paper.update(planned_on="2024-01-01", writing_started_on="2024-02-01", target_journal_id=self.journal)
        self.store.close()
        Path(self.temp.name, "paperdesk.sqlite3").unlink()
        with closing(sqlite3.connect(Path(self.temp.name, "paperdesk.sqlite3"))) as old:
            old.executescript((fixture / "v2-schema.sql").read_text(encoding="utf-8"))
            Store.insert_export(old, data)
            old.commit()
        self.store = Store(Path(self.temp.name), lambda: self.day)
        self.assertEqual(self.store.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        ident = data["papers"][0]["id"]
        self.assertEqual(self.store.paper_detail(ident)["writing_started_on"], "2024-02-01")
        self.assertEqual(self.store.state()["default_authors"], "测试作者")
        snapshot = next(b["name"] for b in self.store.backups() if b["name"].startswith("upgrade-"))
        self.store.journal_save({"name": "原版测试期刊", "guidelines_url": "https://example.com/authors"}, self.journal)
        self.store.restore_snapshot(snapshot)
        self.assertEqual(self.store.require("journals", self.journal)["guidelines_url"], "")
        self.store.restore({"app": "paperdesk-local-v1", "schema_version": 2, "data": data})
        self.assertEqual(self.store.paper_detail(ident)["target_journal_id"], self.journal)

    def test_last_authors_persist_independent_of_submission_changes(self):
        second = self.store.paper_save({"title": "另一篇", "authors": "最近作者", "stage": "writing"})
        self.store.submission_save(self.paper, self.submission())
        self.assertEqual(self.store.state()["default_authors"], "最近作者")
        self.store.paper_save({"title": "另一篇修改", "authors": "修改后的作者", "stage": "writing"}, second)
        self.assertEqual(self.store.state()["default_authors"], "修改后的作者")
        payload = self.store.export()
        self.store.close()
        self.store = Store(Path(self.temp.name), lambda: self.day)
        self.assertEqual(self.store.state()["default_authors"], "修改后的作者")
        self.store.paper_save({"title": "空作者", "stage": "writing"})
        self.store.restore(payload)
        self.assertEqual(self.store.state()["default_authors"], "修改后的作者")

    def test_journal_guidelines_validation_preservation_and_restore(self):
        ident = self.store.journal_save({"name": "须知期刊", "guidelines_url": "https://example.com/authors"})
        self.store.journal_save({"name": "须知期刊改名", "url": "https://example.com/submit"}, ident)
        self.assertEqual(self.store.require("journals", ident)["guidelines_url"], "https://example.com/authors")
        before = self.store.export()
        with self.assertRaisesRegex(ValidationError, "投稿须知链接"):
            self.store.journal_save({"name": "不能覆盖", "guidelines_url": "ftp://example.com"}, ident)
        self.assertEqual(self.store.export()["data"], before["data"])
        self.store.journal_delete(ident)
        self.store.restore(before)
        self.assertEqual(self.store.require("journals", ident)["guidelines_url"], "https://example.com/authors")

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

    def test_paper_code_suggestions_normalization_uniqueness_and_restore(self):
        self.assertEqual(self.store.require("papers", self.paper)["paper_code"], "0001")
        self.assertEqual(self.store.next_paper_code(), "0002")
        second = self.store.paper_save({"title": "自定义编号", "paper_code": " ms-009 ", "stage": "writing"})
        self.assertEqual(self.store.require("papers", second)["paper_code"], "MS-009")
        self.assertEqual(self.store.next_paper_code(), "MS-010")

        with self.assertRaisesRegex(ValidationError, "已存在"):
            self.store.paper_save({"title": "不得重复", "paper_code": "ｍｓ-００９", "stage": "writing"})
        self.assertEqual(len(self.store.state()["papers"]), 2)
        exported = self.store.export()
        self.store.restore(exported)
        self.assertEqual(self.store.export()["data"], exported["data"])
        self.assertEqual(self.store.next_paper_code(), "MS-010")

    def test_very_long_number_suggestion_remains_valid(self):
        self.store.paper_save({"title": "长编号", "stage": "writing", "paper_code": "A" * 63 + "9"})
        suggestion = self.store.next_paper_code()
        self.assertLessEqual(len(suggestion), 64)
        self.store.paper_save({"title": "仍可新建", "stage": "writing", "paper_code": suggestion})

    def test_new_closed_acceptance_syncs_but_history_edits_preserve_manual_stage(self):
        closed = self.submission(submitted_on="2026-09-01", ended_on="2026-10-10", result="accepted")
        ident = self.store.submission_save(self.paper, closed)
        self.assertEqual(self.store.paper_detail(self.paper)["stage"], "accepted")
        self.store.paper_save({"title": "计划重做", "stage": "planned", "planned_on": "2026-09-01"}, self.paper)
        self.store.submission_save(self.paper, {**closed, "notes": "只改旧备注"}, ident)
        self.assertEqual(self.store.paper_detail(self.paper)["stage"], "planned")
        self.store.submission_save(self.paper, {**closed, "sync_stage": True}, ident)
        self.assertEqual(self.store.paper_detail(self.paper)["stage"], "accepted")

    def test_explicit_history_add_does_not_change_current_stage(self):
        closed = self.submission(submitted_on="2026-09-01", ended_on="2026-10-10", result="accepted", historical_only=True)
        self.store.submission_save(self.paper, closed)
        self.assertEqual(self.store.paper_detail(self.paper)["stage"], "writing")

    def test_transfer_is_atomic_keeps_old_round_and_global_identifier(self):
        first = self.submission(submitted_on="2026-09-01", manuscript_number="OLD-J-001", transfer_mode="days", wait_days=30)
        old_id = self.store.submission_save(self.paper, first)
        code = self.store.require("papers", self.paper)["paper_code"]
        old = self.store.require("submissions", old_id)
        paper = self.store.require("papers", self.paper)
        data = {"expected_updated_at": old["updated_at"], "paper_expected_updated_at": paper["updated_at"], "ended_on": "2026-10-13", "result": "rejected", "next_submission": self.submission(journal_id=self.next_journal, manuscript_number="NEW-J-002", candidate_ids=[])}
        result = self.store.transfer_submission(old_id, data)
        detail = self.store.paper_detail(self.paper)
        self.assertEqual(detail["paper_code"], code)
        self.assertEqual(detail["stage"], "submitted")
        self.assertEqual(len(detail["submissions"]), 2)
        ended = next(r for r in detail["submissions"] if r["id"] == old_id)
        active = next(r for r in detail["submissions"] if not r["ended_on"])
        self.assertEqual(ended["result"], "rejected")
        self.assertEqual(ended["elapsed_days"], 42)
        self.assertEqual(ended["manuscript_number"], "OLD-J-001")
        self.assertEqual(ended["target_on"], "2026-10-01")
        self.assertEqual(active["id"], result["id"])
        self.assertEqual(active["elapsed_days"], 0)
        self.assertEqual(active["manuscript_number"], "NEW-J-002")
        self.assertEqual(active["overdue_days"], 0)
        state_paper = next(p for p in self.store.state()["papers"] if p["id"] == self.paper)
        self.assertEqual(state_paper["past_journals"], ["测试期刊"])
        self.assertCountEqual(state_paper["journal_numbers"], ["OLD-J-001", "NEW-J-002"])
        self.day += dt.timedelta(days=1)
        self.assertEqual(next(r for r in self.store.paper_detail(self.paper)["submissions"] if r["id"] == old_id)["elapsed_days"], 42)

    def test_invalid_transfer_rolls_back_old_end_and_all_changes(self):
        old_id = self.store.submission_save(self.paper, self.submission(submitted_on="2026-09-01"))
        before = self.store.export()["data"]
        for next_round in (self.submission(journal_id=999), self.submission(submitted_on="2026-10-01"), self.submission(candidate_ids=[999])):
            with self.subTest(next_round=next_round), self.assertRaises(ValidationError):
                self.store.transfer_submission(old_id, {"ended_on": "2026-10-13", "result": "rejected", "next_submission": next_round})
            self.assertEqual(self.store.export()["data"], before)
        self.store.conn.execute("CREATE TEMP TRIGGER fail_transfer BEFORE INSERT ON submissions BEGIN SELECT RAISE(ABORT, 'simulated disk full'); END")
        try:
            with self.assertRaises(ValidationError):
                self.store.transfer_submission(old_id, {"ended_on": "2026-10-13", "result": "withdrawn", "next_submission": self.submission(journal_id=self.next_journal, candidate_ids=[])})
        finally:
            self.store.conn.execute("DROP TRIGGER fail_transfer")
        self.assertEqual(self.store.export()["data"], before)

    def test_transfer_from_already_closed_round_preserves_its_end(self):
        old = self.submission(submitted_on="2026-09-01", ended_on="2026-10-10", result="rejected")
        old_id = self.store.submission_save(self.paper, old)
        self.store.transfer_submission(old_id, {"next_submission": self.submission(journal_id=self.next_journal, candidate_ids=[])})
        previous = self.store.require("submissions", old_id)
        self.assertEqual(previous["ended_on"], "2026-10-10")
        self.assertEqual(previous["result"], "rejected")

    def test_corrupt_daily_is_preserved_and_replaced_latest_contains_new_record(self):
        daily = self.store.backup_dir / f"daily-{self.day.isoformat()}.sqlite3"
        daily.write_bytes(b"broken")
        self.store.paper_save({"title": "新记录", "stage": "writing"})
        self.assertTrue(any(p.read_bytes() == b"broken" for p in self.store.backup_dir.glob("damaged-*.sqlite3")))
        latest = next(b for b in self.store.backups() if b["name"].startswith("latest-"))
        self.assertEqual(latest["counts"]["papers"], 2)
        self.assertIsNotNone(next(b for b in self.store.backups() if b["name"] == daily.name)["counts"])

    def test_latest_backup_failure_acknowledges_committed_save(self):
        original = self.store.snapshot
        def fail_latest(reason="daily"):
            if reason == "latest":
                raise OSError("simulated disk full")
            return original(reason)
        with patch.object(self.store, "snapshot", side_effect=fail_latest), self.assertLogs(level="ERROR"):
            ident = self.store.paper_save({"title": "记录保存了", "stage": "writing"})
        self.assertEqual(self.store.require("papers", ident)["title"], "记录保存了")
        self.assertIn("记录已保存", self.store.backup_warning)
        self.store.paper_save({"title": "下一次成功", "stage": "writing"})
        self.assertIsNone(self.store.backup_warning)

    def test_stale_edits_rejected_for_paper_journal_and_round(self):
        old = self.store.require("papers", self.paper)
        self.store.paper_save({**old, "title": "另一页改过"}, self.paper)
        with self.assertRaises(ConflictError):
            self.store.paper_save({**old, "notes": "旧表单", "expected_updated_at": old["updated_at"]}, self.paper)
        self.assertEqual(self.store.require("papers", self.paper)["title"], "另一页改过")
        old_j = self.store.require("journals", self.journal)
        self.store.journal_save({**old_j, "notes": "另一页改过"}, self.journal)
        with self.assertRaises(ConflictError):
            self.store.journal_save({**old_j, "expected_updated_at": old_j["updated_at"]}, self.journal)
        ident = self.store.submission_save(self.paper, self.submission())
        old_r = self.store.require("submissions", ident)
        self.store.submission_save(self.paper, self.submission(progress="另一页更新"), ident)
        with self.assertRaises(ConflictError):
            self.store.submission_save(self.paper, self.submission(expected_updated_at=old_r["updated_at"]), ident)

    def test_stale_paper_stage_blocks_round_save_and_delete(self):
        ident = self.store.submission_save(self.paper, self.submission())
        old_paper = self.store.require("papers", self.paper)
        old_round = self.store.require("submissions", ident)
        self.store.paper_save({**old_paper, "stage": "revision"}, self.paper)
        guard = {"expected_updated_at": old_round["updated_at"], "paper_expected_updated_at": old_paper["updated_at"]}
        with self.assertRaises(ConflictError):
            self.store.submission_save(self.paper, self.submission(**guard), ident)
        with self.assertRaises(ConflictError):
            self.store.submission_delete(ident, guard)
        self.assertEqual(self.store.require("papers", self.paper)["stage"], "revision")
        self.assertIsNone(self.store.require("submissions", ident)["ended_on"])

    def test_once_receipt_is_atomic_and_survives_reopen(self):
        key = "operation-create-001"
        payload = {"title": "只创建一次", "stage": "writing"}
        def create():
            return {"id": self.store.paper_save(payload)}
        first = self.store.once(key, "POST:/api/papers", payload, create)
        self.assertEqual(self.store.once(key, "POST:/api/papers", payload, create), first)
        self.store.close()
        self.store = Store(Path(self.temp.name), lambda: self.day)
        self.assertEqual(self.store.once(key, "POST:/api/papers", payload, create), first)
        self.assertEqual(len(self.store.state()["papers"]), 2)
        with self.assertRaises(ConflictError):
            self.store.once(key, "POST:/api/papers", {**payload, "title": "改了内容"}, create)
        def crash_after_insert():
            self.store.paper_save({"title": "不留半条", "stage": "writing"})
            raise OSError("simulated response construction failure")
        before = self.store.export()["data"]
        with self.assertRaises(OSError):
            self.store.once("operation-failure-001", "POST:/api/papers", {}, crash_after_insert)
        self.assertEqual(self.store.export()["data"], before)
        self.assertIsNone(self.store.conn.execute("SELECT 1 FROM operation_receipts WHERE operation_key='operation-failure-001'").fetchone())


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperdesk-migration-")
        self.root = Path(self.temp.name)
        self.target = self.root / "stable"

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, name, title=None):
        directory = self.root / name
        with closing(Store(directory)) as store:
            if title:
                journal = store.journal_save({"name": "测试期刊", "guidelines_url": "https://example.com/authors"})
                store.paper_save({"title": title, "authors": "测试作者", "stage": "submitted", "files": [{"path": str(directory / "稿件.docx")}], "initial_submission": {"journal_id": journal, "submitted_on": "2024-12-31"}})
            return directory, store.export()["data"]

    def test_default_path_independent_of_localappdata_and_discovery_of_redirected_store(self):
        with patch("paperdesk.Path.home", return_value=self.root), patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "normal")}):
            first = default_data_dir()
        with patch("paperdesk.Path.home", return_value=self.root), patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "redirected")}):
            self.assertEqual(default_data_dir(), first)
            self.assertEqual(first, self.root / "PaperDesk")
        normal, _ = self.seed("Local/PaperDesk")
        redirected, _ = self.seed("Local/Packages/OpenAI.Codex_test/LocalCache/Local/PaperDesk", "旧记录")
        with patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "Local")}):
            self.assertEqual(set(legacy_data_dirs()), {normal.resolve(), redirected.resolve()})

    def test_migrate_populated_store_and_backups_without_modifying_either_legacy_store(self):
        empty, _ = self.seed("normal")
        source, expected = self.seed("redirected", "原记录")
        before = (source / "paperdesk.sqlite3").read_bytes()
        prepare_default_storage(self.target, [empty, source])
        with closing(Store(self.target)) as store:
            self.assertEqual(store.export()["data"], expected)
            migrated = next(b for b in store.backups() if b["name"].startswith("migration-"))
            self.assertEqual(migrated["counts"], {"papers": 1, "journals": 1, "submissions": 1, "files": 1})
            store.restore_snapshot(migrated["name"])
            self.assertEqual(store.export()["data"], expected)
        self.assertEqual((source / "paperdesk.sqlite3").read_bytes(), before)
        with closing(Store(empty)) as store:
            self.assertEqual(store.state()["papers"], [])

    def test_conflicting_stores_are_preserved_without_choosing_one(self):
        first, _ = self.seed("first", "记录一")
        second, _ = self.seed("second", "记录二")
        before = [(p / "paperdesk.sqlite3").read_bytes() for p in (first, second)]
        with self.assertRaisesRegex(ValidationError, "多份内容不同"):
            prepare_default_storage(self.target, [first, second])
        self.assertFalse((self.target / "paperdesk.sqlite3").exists())
        self.assertEqual([(p / "paperdesk.sqlite3").read_bytes() for p in (first, second)], before)

    def test_equivalent_old_and_upgraded_databases_migrate_as_one_dataset(self):
        fixture = Path(__file__).parent / "testdata"
        old = self.root / "v1"
        old.mkdir()
        with closing(sqlite3.connect(old / "paperdesk.sqlite3")) as connection:
            connection.executescript((fixture / "v1-schema.sql").read_text(encoding="utf-8"))
            data = json.loads((fixture / "v1-backup.json").read_text(encoding="utf-8"))["data"]
            with connection:
                Store.insert_export(connection, data)
        old_bytes = (old / "paperdesk.sqlite3").read_bytes()
        newer = self.root / "upgraded"
        newer.mkdir()
        (newer / "paperdesk.sqlite3").write_bytes(old_bytes)
        with closing(Store(newer)) as store:
            expected = store.export()["data"]
        prepare_default_storage(self.target, [old, newer])
        with closing(Store(self.target)) as store:
            self.assertEqual(store.export()["data"], expected)
        self.assertEqual((old / "paperdesk.sqlite3").read_bytes(), old_bytes)

    def test_existing_store_never_replaced_or_resurrected_from_old_data(self):
        old, _ = self.seed("legacy", "旧记录")
        self.target, _ = self.seed("stable")
        prepare_default_storage(self.target, [old])
        with closing(Store(self.target)) as store:
            self.assertEqual(store.state()["papers"], [])

    def test_migration_write_failure_preserves_sources_and_does_not_publish_partial_store(self):
        source, _ = self.seed("legacy", "旧记录")
        before = (source / "paperdesk.sqlite3").read_bytes()
        with patch("paperdesk.shutil.copy2", side_effect=OSError("disk full")), self.assertRaises(OSError):
            prepare_default_storage(self.target, [source])
        self.assertFalse((self.target / "paperdesk.sqlite3").exists())
        self.assertEqual((source / "paperdesk.sqlite3").read_bytes(), before)
        prepare_default_storage(self.target, [source])
        with closing(Store(self.target)) as store:
            self.assertEqual(len(store.state()["papers"]), 1)

    def test_corrupt_legacy_database_cannot_silently_start_an_empty_store(self):
        source = self.root / "broken"
        source.mkdir()
        (source / "paperdesk.sqlite3").write_bytes(b"broken database")
        with self.assertRaises(ValidationError):
            prepare_default_storage(self.target, [source])
        self.assertFalse((self.target / "paperdesk.sqlite3").exists())


    def test_zero_byte_store_requires_explicit_validated_recovery(self):
        source, expected = self.seed("source", "保留的记录")
        self.target.mkdir()
        broken = self.target / "paperdesk.sqlite3"
        broken.write_bytes(b"")
        with self.assertRaises(InvalidDatabase):
            prepare_default_storage(self.target, [source])
        with self.assertRaises(InvalidDatabase):
            Store(self.target)
        self.assertEqual(broken.read_bytes(), b"")
        self.assertIsNone(recover_database(self.target, source / "paperdesk.sqlite3", lambda counts: False))
        self.assertEqual(broken.read_bytes(), b"")
        result = recover_database(self.target, source / "paperdesk.sqlite3", lambda counts: counts["papers"] == 1)
        self.assertEqual(result["papers"], 1)
        with closing(Store(self.target)) as store:
            self.assertEqual(store.export()["data"], expected)
        self.assertTrue(list((self.target / "recovery").glob("*/original-database.invalid")))

    def test_v3_schema_upgrade_and_json_preserve_original_data(self):
        source, original = self.seed("v3", "原有数据")
        old_data = copy.deepcopy(original)
        for row in old_data["papers"]:
            row.pop("paper_code")
        database = source / "paperdesk.sqlite3"
        database.unlink()
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript((Path(__file__).parent / "testdata" / "v3-schema.sql").read_text(encoding="utf-8"))
            Store.insert_export(connection, old_data)
            connection.commit()
        with closing(Store(source)) as store:
            self.assertEqual(store.export()["data"], original)
            self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertTrue(any(b["name"].startswith("upgrade-") for b in store.backups()))
            store.restore({"app": "paperdesk-local-v1", "schema_version": 3, "data": old_data})
            self.assertEqual(store.export()["data"], original)

    def test_future_schema_is_not_treated_as_a_damaged_database(self):
        source, _ = self.seed("future", "高版本数据")
        database = source / "paperdesk.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA user_version = 99")
        before = database.read_bytes()
        with self.assertRaises(UnsupportedSchema):
            Store(source)
        with self.assertRaises(UnsupportedSchema):
            prepare_default_storage(source, [])
        self.assertEqual(database.read_bytes(), before)


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
        headers = dict(headers or {})
        if method != "GET":
            headers.setdefault("X-PaperDesk-Client", APP_VERSION)
        req = Request(self.url + path, method=method,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers=headers)
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

    def test_add_existing_submission_in_one_request(self):
        journal = self.store.journal_save({"name": "接口期刊"})
        headers = {"Content-Type": "application/json", "X-PaperDesk-Token": self.server.token, "Origin": self.url}
        code, body, _ = self.request("/api/papers", "POST", {"title": "已投文章", "stage": "submitted", "initial_submission": {"journal_id": journal, "submitted_on": "2024-12-31"}}, headers)
        self.assertEqual(code, 200)
        ident = json.loads(body)["id"]
        detail = self.store.paper_detail(ident)
        self.assertEqual(detail["stage"], "submitted")
        self.assertEqual(detail["submissions"][0]["submitted_on"], "2024-12-31")
        code, _, _ = self.request("/api/papers", "POST", {"title": "无效日期", "stage": "submitted", "initial_submission": {"journal_id": journal, "submitted_on": "2024-02-30"}}, headers)
        self.assertEqual(code, 400)
        self.assertEqual(len(self.store.state()["papers"]), 1)

    def test_port_is_exclusive(self):
        with self.assertRaises(OSError):
            make_server(self.store, self.server.server_port)

    def test_instance_reuse_requires_matching_database_identity(self):
        (self.store.directory / "instance.json").write_text(json.dumps({"port": self.server.server_port}), encoding="utf-8")
        self.assertEqual(existing_url(self.store.directory), self.url)
        other = self.store.directory / "other"
        other.mkdir()
        (other / "instance.json").write_text(json.dumps({"port": self.server.server_port}), encoding="utf-8")
        self.assertIsNone(existing_url(other))
        self.assertNotEqual(storage_id(other), storage_id(self.store.directory))
        with self.assertRaisesRegex(ValidationError, "旧版平台仍在运行"):
            prepare_default_storage(other / "new", [self.store.directory])

    def test_restore_api_validates_preview_reports_counts_and_protects_against_empty_restore(self):
        self.store.paper_save({"title": "保留论文", "stage": "writing"})
        before = self.store.export()
        headers = {"Content-Type": "application/json", "X-PaperDesk-Token": self.server.token, "Origin": self.url}
        empty = next(b["name"] for b in self.store.backups() if b["counts"] and b["counts"]["papers"] == 0)
        code, body, _ = self.request("/api/backups/preview", "POST", {"name": empty}, headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["counts"]["papers"], 0)
        self.assertEqual(self.store.export()["data"], before["data"])
        self.assertEqual(self.request("/api/backups/restore", "POST", {"name": empty}, headers)[0], 400)
        code, body, _ = self.request("/api/restore/preview", "POST", before, headers)
        self.assertEqual(json.loads(body)["counts"]["papers"], 1)
        self.assertEqual(self.request("/api/restore/preview", "POST", {"broken": True}, headers)[0], 400)
        code, body, _ = self.request("/api/backups/restore", "POST", {"name": empty, "allow_empty": True}, headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["counts"]["papers"], 0)
        code, body, _ = self.request("/api/restore", "POST", before, headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["counts"]["papers"], 1)
        self.assertEqual(self.store.export()["data"], before["data"])


    def test_operation_receipt_retries_create_and_put_without_duplicates(self):
        headers = {"Content-Type": "application/json", "X-PaperDesk-Token": self.server.token, "Origin": self.url, "X-PaperDesk-Operation": "create-operation-001"}
        payload = {"title": "只新建一次", "stage": "writing", "paper_code": "A001"}
        first = self.request("/api/papers", "POST", payload, headers)
        second = self.request("/api/papers", "POST", payload, headers)
        self.assertEqual(first[0], 200)
        self.assertEqual(json.loads(first[1])["id"], json.loads(second[1])["id"])
        self.assertEqual(len(self.store.state()["papers"]), 1)
        ident = json.loads(first[1])["id"]
        code, body, _ = self.request("/api/operations/create-operation-001")
        self.assertTrue(json.loads(body)["found"])
        self.assertEqual(self.request("/api/papers", "POST", {**payload, "title": "不能复用标识"}, headers)[0], 409)
        old = self.store.require("papers", ident)
        update = {**old, "notes": "改一次", "expected_updated_at": old["updated_at"]}
        headers["X-PaperDesk-Operation"] = "edit-operation-001"
        self.assertEqual(self.request(f"/api/papers/{ident}", "PUT", update, headers)[0], 200)
        self.assertEqual(self.request(f"/api/papers/{ident}", "PUT", update, headers)[0], 200)
        headers["X-PaperDesk-Operation"] = "stale-operation-001"
        self.assertEqual(self.request(f"/api/papers/{ident}", "PUT", update, headers)[0], 409)

    def test_transfer_request_replay_keeps_exactly_two_rounds(self):
        journal = self.store.journal_save({"name": "原期刊"})
        next_journal = self.store.journal_save({"name": "下一期刊"})
        paper = self.store.paper_save({"title": "转投论文", "stage": "submitted", "initial_submission": {"journal_id": journal, "submitted_on": "2024-12-31", "candidate_ids": [next_journal]}})
        current = self.store.paper_detail(paper)["submissions"][0]
        payload = {"expected_updated_at": current["updated_at"], "paper_expected_updated_at": self.store.require("papers", paper)["updated_at"], "ended_on": "2025-01-02", "result": "rejected", "next_submission": {"journal_id": next_journal, "submitted_on": "2025-01-03"}}
        headers = {"Content-Type": "application/json", "X-PaperDesk-Token": self.server.token, "Origin": self.url, "X-PaperDesk-Operation": "transfer-operation-001"}
        path = f"/api/submissions/{current['id']}/transfer"
        self.assertEqual(self.request(path, "POST", payload, headers)[0], 200)
        self.assertEqual(self.request(path, "POST", payload, headers)[0], 200)
        self.assertEqual(len(self.store.paper_detail(paper)["submissions"]), 2)
        self.assertEqual(sum(not r["ended_on"] for r in self.store.paper_detail(paper)["submissions"]), 1)

    def test_home_state_does_not_scan_backups_and_export_limit_is_explicit(self):
        with patch.object(self.store, "backups", side_effect=AssertionError("Home must not scan all snapshots")):
            self.assertEqual(self.request("/api/state")[0], 200)
        with patch("paperdesk.MAX_JSON_BYTES", 10):
            code, body, _ = self.request("/api/export")
        self.assertEqual(code, 400)
        self.assertIn("恢复上限", json.loads(body)["error"])

    def test_old_page_client_cannot_mutate_new_service(self):
        headers = {"Content-Type": "application/json", "X-PaperDesk-Token": self.server.token, "Origin": self.url, "X-PaperDesk-Client": "1.2.1"}
        self.assertEqual(self.request("/api/papers", "POST", {"title": "旧页面不能新建", "stage": "writing"}, headers)[0], 409)
        headers["X-PaperDesk-Client"] = ""
        self.assertEqual(self.request("/api/papers", "POST", {"title": "缺少页面版本", "stage": "writing"}, headers)[0], 409)
        self.assertEqual(self.store.state()["papers"], [])

    def test_repeat_launch_reports_older_service_version(self):
        (self.store.directory / "instance.json").write_text(json.dumps({"port": self.server.server_port}), encoding="utf-8")
        health = {"app": "paperdesk-local-v1", "version": "1.2.1", "storage_id": storage_id(self.store.directory)}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(health).encode())), self.assertRaisesRegex(ValidationError, "旧版平台"):
            existing_url(self.store.directory)


if __name__ == "__main__":
    unittest.main(verbosity=2)
