"""PaperDesk: a private, offline manuscript tracker. Python standard library only."""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_ID = "paperdesk-local-v1"
SCHEMA_VERSION = 3
ROOT = Path(__file__).resolve().parent
VERSION_PATH = ROOT / "VERSION"
APP_VERSION = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "1.0.0"
DEFAULT_PORT = 8766
CHINA = dt.timezone(dt.timedelta(hours=8))
STAGES = ("planned", "writing", "submitted", "revision", "accepted", "archived")
STAGE_NAMES = dict(zip(STAGES, ("计划中", "正在写", "已投稿", "退修中", "已录用/发表", "归档")))
RESULTS = ("accepted", "rejected", "withdrawn", "other")
TABLES = ("journals", "papers", "files", "submissions", "candidates", "settings")
DOCUMENT_EXTENSIONS = {".doc", ".docx", ".pdf", ".odt", ".rtf", ".txt", ".md", ".tex", ".wps"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS journals (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
 guidelines_url TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS papers (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '',
 tags TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL CHECK(stage IN
 ('planned','writing','submitted','revision','accepted','archived')),
 notes TEXT NOT NULL DEFAULT '', folder TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 planned_on TEXT, writing_started_on TEXT,
 target_journal_id INTEGER REFERENCES journals(id));
CREATE TABLE IF NOT EXISTS files (
 id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 label TEXT NOT NULL, path TEXT NOT NULL, UNIQUE(paper_id,path));
CREATE TABLE IF NOT EXISTS submissions (
 id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 journal_id INTEGER NOT NULL REFERENCES journals(id), submitted_on TEXT NOT NULL,
 manuscript_number TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '', ended_on TEXT, result TEXT CHECK(result IS NULL OR
 result IN ('accepted','rejected','withdrawn','other')),
 transfer_mode TEXT NOT NULL CHECK(transfer_mode IN ('none','days','date')),
 wait_days INTEGER, target_on TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((ended_on IS NULL AND result IS NULL) OR (ended_on IS NOT NULL AND result IS NOT NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_submission ON submissions(paper_id) WHERE ended_on IS NULL;
CREATE TABLE IF NOT EXISTS candidates (
 submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
 journal_id INTEGER NOT NULL REFERENCES journals(id), position INTEGER NOT NULL,
 PRIMARY KEY(submission_id,position), UNIQUE(submission_id,journal_id));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA user_version = 3;
"""


class ValidationError(Exception):
    pass


def today() -> dt.date:
    return dt.datetime.now(CHINA).date()


def timestamp() -> str:
    return dt.datetime.now(CHINA).isoformat(timespec="microseconds")


def text_field(data, key, required=False, limit=10000):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ValidationError(f"{key} 应为文字。")
    value = value.strip()
    if required and not value:
        raise ValidationError("请填写必要的信息。")
    if len(value) > limit or "\x00" in value:
        raise ValidationError("文字过长或包含无效字符。")
    return value


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValidationError("记录编号无效。")
    return value


def parse_date(value, label):
    if not isinstance(value, str) or len(value) != 10:
        raise ValidationError(f"请填写有效的{label}。")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"请填写有效的{label}。") from None
    if parsed.isoformat() != value:
        raise ValidationError(f"请填写有效的{label}。")
    return parsed


def checked_url(value, label="投稿链接"):
    if not value:
        return ""
    if any(c.isspace() for c in value):
        raise ValidationError(f"{label}不能包含空格。")
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError:
        raise ValidationError(f"请填写有效的{label}。") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError(f"{label}须以 http:// 或 https:// 开头。")
    return value


def checked_path(value, folder=False):
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError("请填写完整的本地路径，或使用选择按钮。")
    if not folder and path.suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise ValidationError("稿件支持 Word、PDF、WPS、文本、Markdown、LaTeX 等文档文件。")
    return str(path)


def submission_fields(data, current_day):
    start = parse_date(data.get("submitted_on"), "投稿日期")
    if start > current_day:
        raise ValidationError("投稿日期不能晚于今天；尚未投稿的论文请保留在正在写阶段。")
    end_value = data.get("ended_on") or None
    result = data.get("result") or None
    if bool(end_value) != bool(result):
        raise ValidationError("结束投稿时，请同时填写结束日期和结果。")
    if end_value:
        end = parse_date(end_value, "结束日期")
        if end < start or end > current_day:
            raise ValidationError("结束日期须在投稿日期与今天之间。")
        if result not in RESULTS:
            raise ValidationError("请选择有效的投稿结果。")
    mode = data.get("transfer_mode", "none")
    wait = None
    target = None
    if mode == "days":
        wait = data.get("wait_days")
        if type(wait) is not int or not 1 <= wait <= 3650:
            raise ValidationError("等待天数须为 1 到 3650 的整数。")
        try:
            target = (start + dt.timedelta(days=wait)).isoformat()
        except OverflowError:
            raise ValidationError("转投日期超出了可用范围。") from None
    elif mode == "date":
        target_date = parse_date(data.get("target_on"), "计划转投日期")
        if target_date < start:
            raise ValidationError("计划转投日期不能早于投稿日期。")
        target = target_date.isoformat()
    elif mode != "none":
        raise ValidationError("转投计划类型无效。")
    return {
        "journal_id": positive_id(data.get("journal_id")),
        "submitted_on": start.isoformat(),
        "manuscript_number": text_field(data, "manuscript_number", limit=200),
        "progress": text_field(data, "progress", limit=1000),
        "notes": text_field(data, "notes"),
        "ended_on": end_value, "result": result,
        "transfer_mode": mode, "wait_days": wait, "target_on": target,
    }


class Store:
    def __init__(self, directory: Path, day_provider=today):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.directory / "backups"
        self.backup_dir.mkdir(exist_ok=True)
        self.path = self.directory / "paperdesk.sqlite3"
        self.lock = threading.RLock()
        self.day_provider = day_provider
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        try:
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise ValidationError("数据库版本不兼容，请保留文件并使用对应版本的平台。")
            if version in (1, 2):
                self.snapshot("upgrade")
                migration = ["BEGIN IMMEDIATE;"]
                if version == 1:
                    migration.extend(("ALTER TABLE papers ADD COLUMN planned_on TEXT;",
                                      "ALTER TABLE papers ADD COLUMN writing_started_on TEXT;",
                                      "ALTER TABLE papers ADD COLUMN target_journal_id INTEGER REFERENCES journals(id);"))
                migration.extend(("ALTER TABLE journals ADD COLUMN guidelines_url TEXT NOT NULL DEFAULT '';",
                                  "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
                                  "PRAGMA user_version = 3;", "COMMIT;"))
                self.conn.executescript("\n".join(migration))
            self.conn.executescript(SCHEMA)
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def rows(self, query, args=()):
        return [dict(r) for r in self.conn.execute(query, args).fetchall()]

    def require(self, table, record_id):
        if table not in TABLES or table == "settings":
            raise ValidationError("记录类型无效。")
        record_id = positive_id(record_id)
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise ValidationError("这条记录已不存在，请刷新页面。")
        return dict(row)

    def snapshot(self, reason="daily"):
        day = self.day_provider().isoformat()
        name = f"daily-{day}.sqlite3" if reason == "daily" else f"{reason}-{day}-{time.time_ns()}.sqlite3"
        dest = self.backup_dir / name
        if dest.exists():
            return
        temporary = dest.with_suffix(".tmp")
        try:
            backup = sqlite3.connect(temporary)
            try:
                self.conn.backup(backup)
            finally:
                backup.close()
            temporary.replace(dest)
        finally:
            temporary.unlink(missing_ok=True)
        cutoff = self.day_provider() - dt.timedelta(days=29)
        for item in self.backup_dir.glob("*.sqlite3"):
            try:
                saved_day = dt.date.fromisoformat(item.name.split("-", 1)[1][:10])
            except (ValueError, IndexError):
                continue
            if saved_day < cutoff:
                item.unlink()

    def mutate(self, action, reason="daily"):
        with self.lock:
            self.snapshot(reason)
            try:
                with self.conn:
                    return action()
            except sqlite3.IntegrityError:
                raise ValidationError("记录发生冲突：请检查期刊是否重名，或这篇论文是否已有进行中的投稿。") from None

    def journal_save(self, data, record_id=None):
        name = text_field(data, "name", True, 200)
        url = checked_url(text_field(data, "url", limit=3000))
        notes = text_field(data, "notes")
        guidelines = checked_url(text_field(data, "guidelines_url", limit=3000), "投稿须知链接") if "guidelines_url" in data else None
        def action():
            if record_id:
                previous = self.require("journals", record_id)
                self.conn.execute("UPDATE journals SET name=?,url=?,notes=?,updated_at=?,guidelines_url=? WHERE id=?",
                                  (name, url, notes, timestamp(), guidelines if guidelines is not None else previous["guidelines_url"], record_id))
                return record_id
            return self.conn.execute("INSERT INTO journals(name,url,notes,updated_at,guidelines_url) VALUES(?,?,?,?,?)",
                                     (name, url, notes, timestamp(), guidelines or "")).lastrowid
        return self.mutate(action)

    def journal_delete(self, record_id):
        def action():
            self.require("journals", record_id)
            if self.conn.execute("SELECT 1 FROM submissions WHERE journal_id=? UNION SELECT 1 FROM candidates WHERE journal_id=? UNION SELECT 1 FROM papers WHERE target_journal_id=?",
                                 (record_id, record_id, record_id)).fetchone():
                raise ValidationError("这个期刊已用于初始目标、投稿或转投计划，请先修改相关记录。")
            self.conn.execute("DELETE FROM journals WHERE id=?", (record_id,))
        self.mutate(action, "delete")

    def paper_save(self, data, record_id=None):
        fields = {"title": text_field(data, "title", True, 500),
                  "authors": text_field(data, "authors", limit=1000),
                  "tags": text_field(data, "tags", limit=1000),
                  "stage": data.get("stage", "writing"), "notes": text_field(data, "notes"),
                  "folder": checked_path(text_field(data, "folder", limit=4000), True)}
        if fields["stage"] not in STAGES:
            raise ValidationError("请选择有效的论文阶段。")
        initial = data.get("initial_submission")
        if initial is not None:
            if record_id or fields["stage"] not in ("submitted", "revision") or not isinstance(initial, dict):
                raise ValidationError("首轮投稿只能随新建的已投稿或退修中论文一起登记。")
            if initial.get("ended_on") or initial.get("result"):
                raise ValidationError("直接添加已投稿论文时，首轮投稿应为进行中。")
        for key, label in (("planned_on", "计划建立日期"), ("writing_started_on", "开始撰写日期")):
            if key in data:
                value = data[key]
                if value is not None and value != "":
                    date = parse_date(value, label)
                    if date > self.day_provider():
                        raise ValidationError(f"{label}不能晚于今天。")
                    fields[key] = date.isoformat()
                else:
                    fields[key] = None
        if "target_journal_id" in data:
            fields["target_journal_id"] = None if data["target_journal_id"] in (None, "") else positive_id(data["target_journal_id"])
        files = data.get("files", [])
        if not isinstance(files, list) or len(files) > 100:
            raise ValidationError("稿件文件列表无效。")
        normalized = []
        for entry in files:
            if not isinstance(entry, dict):
                raise ValidationError("稿件文件信息无效。")
            path = checked_path(text_field(entry, "path", True, 4000))
            label = text_field(entry, "label", limit=200) or Path(path).name
            normalized.append((label, path))
        if len({os.path.normcase(p) for _, p in normalized}) != len(normalized):
            raise ValidationError("同一个稿件文件只需关联一次。")
        def action():
            nonlocal record_id
            active = None
            previous = None
            if record_id:
                previous = self.require("papers", record_id)
                active = self.conn.execute("SELECT id FROM submissions WHERE paper_id=? AND ended_on IS NULL", (record_id,)).fetchone()
            if fields["stage"] in ("submitted", "revision") and not active and initial is None:
                raise ValidationError("新建已投稿论文时请填写投稿期刊与日期；已有论文请在详情中登记投稿。")
            if active and fields["stage"] not in ("submitted", "revision", "archived"):
                raise ValidationError("请先结束当前投稿；录用时选择结果“录用”，平台会自动更新阶段。")
            for key, phase in (("planned_on", "planned"), ("writing_started_on", "writing")):
                if key not in fields:
                    value = previous[key] if previous else None
                    if not value and fields["stage"] == phase and (not previous or previous["stage"] != phase):
                        value = self.day_provider().isoformat()
                    fields[key] = value
            fields.setdefault("target_journal_id", previous["target_journal_id"] if previous else None)
            if fields["target_journal_id"] is not None:
                self.require("journals", fields["target_journal_id"])
            if fields["planned_on"] and fields["writing_started_on"] and fields["writing_started_on"] < fields["planned_on"]:
                raise ValidationError("开始撰写日期不能早于计划建立日期。")
            stamp = timestamp()
            if record_id:
                assignments = ",".join(f"{key}=?" for key in fields)
                self.conn.execute(f"UPDATE papers SET {assignments},updated_at=? WHERE id=?",
                                  (*fields.values(), stamp, record_id))
            else:
                columns = ",".join(fields)
                placeholders = ",".join("?" for _ in fields)
                record_id = self.conn.execute(f"INSERT INTO papers({columns},created_at,updated_at) VALUES({placeholders},?,?)",
                                              (*fields.values(), stamp, stamp)).lastrowid
            self.conn.execute("DELETE FROM files WHERE paper_id=?", (record_id,))
            self.conn.executemany("INSERT INTO files(paper_id,label,path) VALUES(?,?,?)",
                                  [(record_id, label, path) for label, path in normalized])
            if initial is not None:
                # Both records share this transaction; a failed round leaves no partial paper.
                self._submission_action(record_id, initial)()
                self.conn.execute("UPDATE papers SET stage=? WHERE id=?", (fields["stage"], record_id))
            if fields["authors"]:
                self.conn.execute("INSERT INTO settings(key,value) VALUES('default_authors',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fields["authors"],))
            return record_id
        return self.mutate(action)

    def paper_delete(self, record_id):
        def action():
            self.require("papers", record_id)
            self.conn.execute("DELETE FROM papers WHERE id=?", (record_id,))
        self.mutate(action, "delete")

    def submission_save(self, paper_id, data, record_id=None):
        return self.mutate(self._submission_action(paper_id, data, record_id))

    def _submission_action(self, paper_id, data, record_id=None):
        fields = submission_fields(data, self.day_provider())
        candidates = data.get("candidate_ids", [])
        if not isinstance(candidates, list) or len(candidates) > 100:
            raise ValidationError("候选期刊列表无效。")
        candidates = [positive_id(i) for i in candidates]
        if len(set(candidates)) != len(candidates):
            raise ValidationError("候选期刊不能重复。")
        def action():
            nonlocal record_id
            paper = self.require("papers", paper_id)
            self.require("journals", fields["journal_id"])
            for journal_id in candidates:
                self.require("journals", journal_id)
            previous = self.require("submissions", record_id) if record_id else None
            if previous and previous["paper_id"] != paper_id:
                raise ValidationError("投稿记录与论文不对应。")
            if fields["ended_on"] is None and self.conn.execute(
                    "SELECT id FROM submissions WHERE paper_id=? AND ended_on IS NULL AND id<>?",
                    (paper_id, record_id or 0)).fetchone():
                raise ValidationError("请先结束上一轮投稿，再登记新的进行中投稿。")
            stamp = timestamp()
            if record_id:
                assignments = ",".join(f"{key}=?" for key in fields)
                self.conn.execute(f"UPDATE submissions SET {assignments},updated_at=? WHERE id=?",
                                  (*fields.values(), stamp, record_id))
            else:
                columns = ",".join(fields)
                placeholders = ",".join("?" for _ in fields)
                record_id = self.conn.execute(
                    f"INSERT INTO submissions(paper_id,{columns},created_at,updated_at) VALUES(?,{placeholders},?,?)",
                    (paper_id, *fields.values(), stamp, stamp)).lastrowid
            self.conn.execute("DELETE FROM candidates WHERE submission_id=?", (record_id,))
            self.conn.executemany("INSERT INTO candidates(submission_id,journal_id,position) VALUES(?,?,?)",
                                  [(record_id, j, pos) for pos, j in enumerate(candidates)])
            phase = paper["stage"]
            if fields["ended_on"] is None:
                if not previous or previous["ended_on"] is not None or phase not in ("revision", "archived"):
                    phase = "submitted"
            elif previous and previous["ended_on"] is None and phase != "archived":
                phase = "accepted" if fields["result"] == "accepted" else "writing"
            elif previous and phase != "archived":
                latest = self.conn.execute("SELECT id FROM submissions WHERE paper_id=? ORDER BY submitted_on DESC,id DESC LIMIT 1", (paper_id,)).fetchone()
                active = self.conn.execute("SELECT 1 FROM submissions WHERE paper_id=? AND ended_on IS NULL", (paper_id,)).fetchone()
                if latest and latest[0] == record_id and not active:
                    phase = "accepted" if fields["result"] == "accepted" else "writing"
            self.conn.execute("UPDATE papers SET stage=?,updated_at=? WHERE id=?", (phase, stamp, paper_id))
            return record_id
        return action

    def submission_delete(self, record_id):
        def action():
            record = self.require("submissions", record_id)
            paper = self.require("papers", record["paper_id"])
            self.conn.execute("DELETE FROM submissions WHERE id=?", (record_id,))
            phase = paper["stage"]
            if record["ended_on"] is None and phase != "archived":
                phase = "writing"
            self.conn.execute("UPDATE papers SET stage=?,updated_at=? WHERE id=?", (phase, timestamp(), paper["id"]))
        self.mutate(action, "delete")

    def decorate_submission(self, record, stage):
        record = dict(record)
        journal = self.require("journals", record["journal_id"])
        record["journal_name"] = journal["name"]
        record["journal_url"] = journal["url"]
        record["candidates"] = self.rows("SELECT j.id,j.name,j.url FROM candidates c JOIN journals j ON j.id=c.journal_id WHERE c.submission_id=? ORDER BY c.position", (record["id"],))
        current = self.day_provider()
        end = dt.date.fromisoformat(record["ended_on"]) if record["ended_on"] else current
        start = dt.date.fromisoformat(record["submitted_on"])
        record["elapsed_days"] = (end - start).days
        due = dt.date.fromisoformat(record["target_on"]) if record["target_on"] else None
        record["overdue_days"] = max(0, (current - due).days) if due and not record["ended_on"] and stage not in ("accepted", "archived") else 0
        return record

    def decorate_paper(self, paper):
        paper["target_journal"] = self.require("journals", paper["target_journal_id"]) if paper["target_journal_id"] else None
        key = {"planned": "planned_on", "writing": "writing_started_on"}.get(paper["stage"])
        paper["stage_started_on"] = paper[key] if key else None
        paper["stage_elapsed_days"] = (self.day_provider() - dt.date.fromisoformat(paper[key])).days if key and paper[key] else None
        return paper

    def paper_detail(self, record_id):
        with self.lock:
            paper = self.decorate_paper(self.require("papers", record_id))
            paper["files"] = self.rows("SELECT * FROM files WHERE paper_id=? ORDER BY id", (record_id,))
            paper["submissions"] = [self.decorate_submission(r, paper["stage"]) for r in self.rows(
                "SELECT * FROM submissions WHERE paper_id=? ORDER BY submitted_on DESC,id DESC", (record_id,))]
            return paper

    def default_authors(self):
        row = self.conn.execute("SELECT value FROM settings WHERE key='default_authors'").fetchone()
        if row:
            return row[0]
        row = self.conn.execute("SELECT authors FROM papers WHERE trim(authors)<>'' ORDER BY updated_at DESC,id DESC LIMIT 1").fetchone()
        return row[0] if row else ""

    def state(self):
        with self.lock:
            papers = self.rows("SELECT * FROM papers ORDER BY updated_at DESC,id DESC")
            counts = {phase: 0 for phase in STAGES}
            for paper in papers:
                self.decorate_paper(paper)
                counts[paper["stage"]] += 1
                rounds = self.rows("SELECT * FROM submissions WHERE paper_id=? ORDER BY submitted_on DESC,id DESC", (paper["id"],))
                active = next((r for r in rounds if not r["ended_on"]), None)
                current = active or (rounds[0] if rounds else None)
                paper["current_submission"] = self.decorate_submission(current, paper["stage"]) if current else None
                paper["round_count"] = len(rounds)
                paper["overdue_days"] = paper["current_submission"]["overdue_days"] if current else 0
            journals = self.rows("SELECT * FROM journals ORDER BY name COLLATE NOCASE,id")
            for journal in journals:
                journal["usage_count"] = self.conn.execute("SELECT COUNT(*) FROM submissions WHERE journal_id=?", (journal["id"],)).fetchone()[0]
            return {"papers": papers, "journals": journals, "counts": counts,
                    "today": self.day_provider().isoformat(), "overdue_count": sum(p["overdue_days"] > 0 for p in papers),
                    "data_dir": str(self.directory), "database_path": str(self.path),
                    "backup_dir": str(self.backup_dir), "backups": self.backups(), "app_version": APP_VERSION,
                    "default_authors": self.default_authors()}

    def export(self):
        with self.lock:
            return {"app": APP_ID, "app_version": APP_VERSION, "schema_version": SCHEMA_VERSION, "exported_at": timestamp(),
                    "data": {table: self.rows(f"SELECT * FROM {table} ORDER BY " + ("submission_id,position" if table == "candidates" else "key" if table == "settings" else "id")) for table in TABLES}}

    def validate_export(self, payload):
        if not isinstance(payload, dict) or payload.get("app") != APP_ID or type(payload.get("schema_version")) is not int or payload["schema_version"] not in (1, 2, SCHEMA_VERSION):
            raise ValidationError("这不是本平台支持的备份文件。")
        data = payload.get("data")
        expected_tables = TABLES if payload["schema_version"] == SCHEMA_VERSION else TABLES[:-1]
        if not isinstance(data, dict) or set(data) != set(expected_tables):
            raise ValidationError("备份缺少必要的数据表。")
        for name in expected_tables:
            if not isinstance(data[name], list) or len(data[name]) > 100000:
                raise ValidationError("备份数据格式或大小无效。")
        data = {name: [dict(row) if isinstance(row, dict) else row for row in data[name]] for name in expected_tables}
        data.setdefault("settings", [])
        setting_keys = set()
        for row in data["settings"]:
            if not isinstance(row, dict) or row.get("key") != "default_authors" or row["key"] in setting_keys:
                raise ValidationError("备份中的作者默认设置无效。")
            setting_keys.add(row["key"])
            text_field(row, "value", limit=1000)
        journals = set()
        papers = {}
        rounds = {}
        file_ids = set()
        for row in data["journals"]:
            if not isinstance(row, dict):
                raise ValidationError("期刊备份格式无效。")
            ident = positive_id(row.get("id"))
            if ident in journals:
                raise ValidationError("备份包含重复记录。")
            journals.add(ident)
            if payload["schema_version"] < 3:
                row.setdefault("guidelines_url", "")
            text_field(row, "name", True, 200)
            checked_url(text_field(row, "url", limit=3000))
            checked_url(text_field(row, "guidelines_url", limit=3000), "投稿须知链接")
            text_field(row, "notes")
            text_field(row, "updated_at", True, 100)
        for row in data["papers"]:
            if not isinstance(row, dict):
                raise ValidationError("论文备份格式无效。")
            if payload["schema_version"] == 1:
                for key in ("planned_on", "writing_started_on", "target_journal_id"):
                    row.setdefault(key, None)
            ident = positive_id(row.get("id"))
            if ident in papers or row.get("stage") not in STAGES:
                raise ValidationError("备份包含重复论文或无效阶段。")
            papers[ident] = row
            text_field(row, "title", True, 500)
            text_field(row, "authors", limit=1000)
            text_field(row, "tags", limit=1000)
            text_field(row, "notes")
            checked_path(text_field(row, "folder", limit=4000), True)
            for key in ("created_at", "updated_at"):
                text_field(row, key, True, 100)
            for key, label in (("planned_on", "计划建立日期"), ("writing_started_on", "开始撰写日期")):
                if row.get(key) is not None and parse_date(row[key], label) > self.day_provider():
                    raise ValidationError(f"备份中的{label}不能晚于今天。")
            if row.get("planned_on") and row.get("writing_started_on") and row["writing_started_on"] < row["planned_on"]:
                raise ValidationError("备份中的开始撰写日期早于计划建立日期。")
            if row.get("target_journal_id") is not None and positive_id(row["target_journal_id"]) not in journals:
                raise ValidationError("备份中的初始目标期刊关联无效。")
        active_papers = set()
        for row in data["submissions"]:
            if not isinstance(row, dict):
                raise ValidationError("投稿备份格式无效。")
            ident = positive_id(row.get("id"))
            paper_ref = positive_id(row.get("paper_id"))
            journal_ref = positive_id(row.get("journal_id"))
            if ident in rounds or paper_ref not in papers or journal_ref not in journals:
                raise ValidationError("备份中的投稿关联无效。")
            fields = submission_fields(row, self.day_provider())
            if fields["target_on"] != row.get("target_on") or fields["wait_days"] != row.get("wait_days"):
                raise ValidationError("备份中的转投日期与等待天数不一致。")
            rounds[ident] = row
            if not row.get("ended_on"):
                if row["paper_id"] in active_papers:
                    raise ValidationError("备份中同一论文存在多轮进行中投稿。")
                active_papers.add(row["paper_id"])
            for key in ("created_at", "updated_at"):
                text_field(row, key, True, 100)
        for ident, row in papers.items():
            if ident in active_papers and row["stage"] not in ("submitted", "revision", "archived"):
                raise ValidationError("备份中论文阶段与进行中的投稿不一致。")
            if ident not in active_papers and row["stage"] in ("submitted", "revision"):
                raise ValidationError("备份中已投稿或退修论文缺少进行中的投稿。")
        for row in data["files"]:
            if not isinstance(row, dict):
                raise ValidationError("稿件关联备份格式无效。")
            ident = positive_id(row.get("id"))
            if ident in file_ids or positive_id(row.get("paper_id")) not in papers:
                raise ValidationError("备份中的稿件关联无效。")
            file_ids.add(ident)
            checked_path(text_field(row, "path", True, 4000))
            text_field(row, "label", True, 200)
        positions = {}
        candidate_keys = set()
        for row in data["candidates"]:
            if not isinstance(row, dict):
                raise ValidationError("备份中的候选期刊关联无效。")
            if positive_id(row.get("submission_id")) not in rounds or positive_id(row.get("journal_id")) not in journals:
                raise ValidationError("备份中的候选期刊关联无效。")
            position = row.get("position")
            key = (row["submission_id"], row["journal_id"])
            if type(position) is not int or position < 0 or key in candidate_keys:
                raise ValidationError("备份中的候选期刊顺序无效。")
            candidate_keys.add(key)
            positions.setdefault(row["submission_id"], []).append(position)
        if any(sorted(values) != list(range(len(values))) for values in positions.values()):
            raise ValidationError("备份中的候选期刊顺序不连续。")
        return data

    def restore(self, payload):
        with self.lock:
            data = self.validate_export(payload)
            # Validate the exact SQL representation in isolation before touching the live database.
            probe = sqlite3.connect(":memory:")
            try:
                probe.execute("PRAGMA foreign_keys=ON")
                probe.executescript(SCHEMA)
                self.insert_export(probe, data)
                if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValidationError("备份完整性检查未通过。")
            except (sqlite3.Error, KeyError, TypeError, ValueError):
                raise ValidationError("备份内容不完整或存在冲突，当前数据没有被修改。") from None
            finally:
                probe.close()
            def action():
                for table in reversed(TABLES):
                    self.conn.execute(f"DELETE FROM {table}")
                self.insert_export(self.conn, data)
            self.mutate(action, "restore")

    @staticmethod
    def insert_export(connection, data):
        for table in TABLES:
            if table not in data:
                continue
            columns = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
            query = f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})"
            for row in data[table]:
                if set(row) != set(columns):
                    raise ValidationError("备份字段与当前版本不一致。")
                connection.execute(query, [row[c] for c in columns])

    def backups(self):
        return [{"name": path.name, "size": path.stat().st_size,
                 "modified_at": dt.datetime.fromtimestamp(path.stat().st_mtime, CHINA).isoformat(timespec="seconds")}
                for path in sorted(self.backup_dir.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)]

    def restore_snapshot(self, name):
        with self.lock:
            if not isinstance(name, str) or name not in {b["name"] for b in self.backups()}:
                raise ValidationError("自动备份不存在，请刷新列表。")
            path = self.backup_dir / name
            connection = None
            try:
                connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                connection.row_factory = sqlite3.Row
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or version not in (1, 2, SCHEMA_VERSION):
                    raise ValidationError("自动备份损坏或版本不兼容。")
                payload = {"app": APP_ID, "schema_version": version,
                           "data": {table: [dict(r) for r in connection.execute(f"SELECT * FROM {table}")] for table in (TABLES if version == SCHEMA_VERSION else TABLES[:-1])}}
            except sqlite3.Error:
                raise ValidationError("无法读取这份备份，当前数据没有被修改。") from None
            finally:
                if connection:
                    connection.close()
            self.restore(payload)

    def open_registered(self, kind, ident):
        with self.lock:
            if kind == "file":
                path = Path(self.require("files", ident)["path"])
                if not path.is_file():
                    raise ValidationError("稿件不存在或已移动，请在论文信息中重新关联。")
                checked_path(str(path))
            elif kind == "paper":
                value = self.require("papers", ident)["folder"]
                if not value or not Path(value).is_dir():
                    raise ValidationError("论文文件夹不存在或已移动，请重新关联。")
                path = Path(value)
            elif kind == "storage":
                path = self.backup_dir if ident == "backups" else self.directory
            else:
                raise ValidationError("无法打开这个文件。")
        try:
            os.startfile(str(path))
        except OSError:
            raise ValidationError("Windows 无法打开此文件，请检查是否安装了对应的阅读软件。") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperDesk"

    def log_message(self, format, *args):
        logging.info(format, *args)

    def reply(self, status, payload, mime="application/json; charset=utf-8", attachment=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(payload)

    def trusted(self, mutation=False):
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host:
            self.reply(403, {"error": "请通过平台的本机启动入口访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            self.reply(403, {"error": "不支持来自其他网站的请求。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.reply(403, {"error": "不支持来自其他网站的请求。"})
            return False
        if mutation and not secrets.compare_digest(self.headers.get("X-PaperDesk-Token", ""), self.server.token):
            self.reply(403, {"error": "页面连接已更新，请刷新后重试。"})
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        route = urlparse(self.path).path
        store = self.server.store
        try:
            if route == "/api/health":
                self.reply(200, {"app": APP_ID, "version": APP_VERSION, "status": "ok"})
            elif route == "/api/state":
                self.reply(200, {**store.state(), "token": self.server.token})
            elif route.startswith("/api/papers/") and route.rsplit("/", 1)[1].isdigit():
                self.reply(200, store.paper_detail(int(route.rsplit("/", 1)[1])))
            elif route == "/api/export":
                self.reply(200, store.export(), attachment=f"paperdesk-backup-{today().isoformat()}.json")
            elif route in ("/", "/index.html", "/app.js", "/style.css", "/interaction.css", "/icons.svg", "/favicon.svg"):
                filename = "index.html" if route == "/" else route[1:]
                types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
                self.reply(200, (ROOT / "web" / filename).read_bytes(), types[Path(filename).suffix])
            else:
                self.reply(404, {"error": "页面或记录不存在。"})
        except ValidationError as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:
            logging.exception("GET failed")
            self.reply(500, {"error": "读取失败，请重试；数据保留在本机。"})

    def read_body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValidationError("请求内容无效。") from None
        if not 0 < size <= 20 * 1024 * 1024:
            raise ValidationError("请求为空或超过 20 MB。")
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ValidationError("请求格式须为 JSON。")
        try:
            body = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ValidationError("内容不是有效的 JSON。") from None
        if not isinstance(body, dict):
            raise ValidationError("请求内容格式无效。")
        return body

    def do_POST(self):
        self.mutation()

    def do_PUT(self):
        self.mutation()

    def do_DELETE(self):
        self.mutation()

    def mutation(self):
        if not self.trusted(True):
            return
        try:
            data = self.read_body()
            parts = urlparse(self.path).path.strip("/").split("/")
            store = self.server.store
            result = {}
            if parts == ["api", "journals"] and self.command == "POST":
                result["id"] = store.journal_save(data)
            elif len(parts) == 3 and parts[:2] == ["api", "journals"] and parts[2].isdigit():
                ident = int(parts[2])
                if self.command == "PUT":
                    result["id"] = store.journal_save(data, ident)
                elif self.command == "DELETE":
                    store.journal_delete(ident)
                else:
                    raise ValidationError("操作方式无效。")
            elif parts == ["api", "papers"] and self.command == "POST":
                result["id"] = store.paper_save(data)
            elif len(parts) == 3 and parts[:2] == ["api", "papers"] and parts[2].isdigit():
                ident = int(parts[2])
                if self.command == "PUT":
                    result["id"] = store.paper_save(data, ident)
                elif self.command == "DELETE":
                    store.paper_delete(ident)
                else:
                    raise ValidationError("操作方式无效。")
            elif len(parts) == 4 and parts[:2] == ["api", "papers"] and parts[2].isdigit() and parts[3] == "submissions" and self.command == "POST":
                result["id"] = store.submission_save(int(parts[2]), data)
            elif len(parts) == 3 and parts[:2] == ["api", "submissions"] and parts[2].isdigit():
                ident = int(parts[2])
                if self.command == "PUT":
                    with store.lock:
                        paper_id = store.require("submissions", ident)["paper_id"]
                        result["id"] = store.submission_save(paper_id, data, ident)
                elif self.command == "DELETE":
                    store.submission_delete(ident)
                else:
                    raise ValidationError("操作方式无效。")
            elif parts == ["api", "restore"] and self.command == "POST":
                store.restore(data)
            elif parts == ["api", "backups", "restore"] and self.command == "POST":
                store.restore_snapshot(data.get("name"))
            elif parts == ["api", "open"] and self.command == "POST":
                store.open_registered(data.get("kind"), data.get("id"))
            elif parts == ["api", "pick"] and self.command == "POST":
                kind = data.get("kind")
                if kind not in ("folder", "file"):
                    raise ValidationError("选择类型无效。")
                executable = Path(sys.executable)
                if executable.name.lower() == "pythonw.exe":
                    executable = executable.with_name("python.exe")
                proc = subprocess.run([str(executable), str(ROOT / "pick_path.py"), kind], capture_output=True,
                                      encoding="utf-8", timeout=600,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode:
                    raise ValidationError("无法打开文件选择窗口，请直接粘贴完整路径。")
                result = json.loads(proc.stdout)
            elif parts == ["api", "shutdown"] and self.command == "POST":
                self.reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                self.reply(404, {"error": "操作不存在。"})
                return
            self.reply(200, {"ok": True, **result})
        except ValidationError as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:
            logging.exception("Mutation failed")
            self.reply(500, {"error": "保存或操作失败，当前数据未被覆盖。请重试或查看运行日志。"})


def default_data_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PaperDesk"


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(store, port=0):
    server = LocalServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.store = store
    server.token = secrets.token_urlsafe(32)
    return server


def existing_url(directory):
    from urllib.request import urlopen
    try:
        state = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        port = state["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        url = f"http://127.0.0.1:{port}"
        with urlopen(url + "/api/health", timeout=1) as response:
            payload = json.load(response)
            return url if payload.get("app") == APP_ID else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # Keep the nominal user path stable across packaged and normal Windows launches.
    # resolve() can rewrite LocalAppData into an MSIX virtualization path.
    directory = args.data_dir.absolute()
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=directory / "paperdesk.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    mutex = None
    if os.name == "nt":
        import hashlib
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        key = hashlib.sha256(str(directory).lower().encode()).hexdigest()[:24]
        mutex = kernel.CreateMutexW(None, False, f"Local\\PaperDesk-{key}")
        if not mutex:
            raise OSError("无法创建启动锁。")
        already = ctypes.get_last_error() == 183
        if already:
            try:
                for _ in range(20):
                    url = existing_url(directory)
                    if url:
                        if not args.no_browser:
                            webbrowser.open(url)
                        if sys.stdout:
                            print(url)
                        return
                    time.sleep(0.25)
                raise RuntimeError("已有平台正在启动或尚未退出，请稍后再试。")
            finally:
                kernel.CloseHandle(mutex)
    store = None
    server = None
    try:
        store = Store(directory)
        for port in range(args.port, args.port + 20):
            try:
                server = make_server(store, port)
                break
            except OSError:
                continue
        if not server:
            raise RuntimeError("本机端口不可用，请关闭其他平台实例后重试。")
        statefile = directory / "instance.json"
        temporary = statefile.with_suffix(".tmp")
        temporary.write_text(json.dumps({"app": APP_ID, "port": server.server_port}), encoding="utf-8")
        temporary.replace(statefile)
        url = f"http://127.0.0.1:{server.server_port}"
        if sys.stdout:
            print(url, flush=True)
        if not args.no_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        server.serve_forever(poll_interval=0.2)
    finally:
        if server:
            server.server_close()
            (directory / "instance.json").unlink(missing_ok=True)
        if store:
            store.close()
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
