CREATE TABLE IF NOT EXISTS journals (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS papers (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '',
 tags TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL CHECK(stage IN
 ('planned','writing','submitted','revision','accepted','archived')),
 notes TEXT NOT NULL DEFAULT '', folder TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
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
PRAGMA user_version = 1;
