"""SQLite is the source of truth for discovery, jobs and publication attempts."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import hashlib
from pathlib import Path
import sqlite3
import uuid
from .config import TZ, digest, now_iso


class StateError(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,cursor TEXT,health TEXT,last_success TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY,source_id TEXT NOT NULL,article_key TEXT NOT NULL,
          status TEXT NOT NULL,article TEXT NOT NULL,source_hash TEXT,draft TEXT,content_hash TEXT,
          review TEXT,artifacts TEXT,revision INTEGER NOT NULL DEFAULT 0,reason TEXT,
          retry_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          UNIQUE(source_id,article_key));
        CREATE TABLE IF NOT EXISTS attempts(
          attempt_id TEXT PRIMARY KEY,job_id TEXT UNIQUE NOT NULL REFERENCES jobs(id),
          account_id TEXT NOT NULL,title TEXT NOT NULL,content_hash TEXT NOT NULL,
          policy_hash TEXT NOT NULL,status TEXT NOT NULL,intent_at TEXT NOT NULL,
          submitted_at TEXT,observation TEXT,account_evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_locks(account_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aliases(source_id TEXT NOT NULL,alias TEXT NOT NULL,job_id TEXT NOT NULL REFERENCES jobs(id),PRIMARY KEY(source_id,alias));
        CREATE TABLE IF NOT EXISTS feed_snapshots(
          source_id TEXT NOT NULL,article_key TEXT NOT NULL,signature TEXT NOT NULL,
          payload TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(source_id,article_key));
        CREATE TABLE IF NOT EXISTS run_leases(name TEXT PRIMARY KEY,token TEXT NOT NULL,owner TEXT NOT NULL,expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
          at TEXT NOT NULL,kind TEXT NOT NULL,job_id TEXT,details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_outlines(job_id TEXT PRIMARY KEY REFERENCES jobs(id),
          source_hash TEXT NOT NULL,outline_hash TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS image_plans(job_id TEXT PRIMARY KEY REFERENCES jobs(id),
          creative_hash TEXT NOT NULL,source_hash TEXT NOT NULL,plan_hash TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS image_jobs(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),
          creative_hash TEXT NOT NULL,plan_hash TEXT NOT NULL,role TEXT NOT NULL,image_order INTEGER NOT NULL,
          state TEXT NOT NULL,payload TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,
          repair_count INTEGER NOT NULL DEFAULT 0,result TEXT,inspection TEXT,reason TEXT,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(job_id,plan_hash,role));
        CREATE TABLE IF NOT EXISTS image_attempts(id TEXT PRIMARY KEY,image_id TEXT NOT NULL REFERENCES image_jobs(id),
          attempt_number INTEGER NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,
          UNIQUE(image_id,attempt_number));
        """)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','2')")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def event(self, kind, job_id=None, **details):
        self.db.execute("INSERT INTO events(at,kind,job_id,details) VALUES(?,?,?,?)",
                        (now_iso(), kind, job_id, json.dumps(details, ensure_ascii=False)))

    def paused(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()
        return bool(row and row[0] == "true")

    def set_paused(self, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('paused',?)", ("true" if value else "false",))
        self.event("paused" if value else "resumed")

    def source_state(self, source_id):
        row = self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else {}

    def begin_run(self, owner, minutes=120):
        if not isinstance(owner, str) or not owner.strip():
            raise StateError("Run owner must be an explicit non-empty identifier")
        with self.transaction():
            old = self.db.execute("SELECT * FROM run_leases WHERE name='codex'").fetchone()
            if old and old["expires_at"] > now_iso():
                raise StateError("Another Codex execution is active; do not process the same queue")
            token = uuid.uuid4().hex
            expires = (datetime.now(TZ) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            self.db.execute("INSERT OR REPLACE INTO run_leases VALUES('codex',?,?,?)", (token, owner, expires))
            return {"lease_token": token, "owner": owner, "expires_at": expires}

    def check_run(self, token=None, required=False):
        row = self.db.execute("SELECT * FROM run_leases WHERE name='codex'").fetchone()
        valid = bool(row and row["expires_at"] > now_iso())
        if (valid and row["token"] != token) or (required and not valid):
            raise StateError("An active execution lease is required; use begin-run and its token")
        if token and (not valid or row["token"] != token):
            raise StateError("Execution lease expired or changed; stop external actions")

    def end_run(self, token):
        self.check_run(token, required=True)
        self.db.execute("DELETE FROM run_leases WHERE name='codex' AND token=?", (token,))
        return {"released": True}

    def import_article(self, source_id, article):
        key = article["article_id"]
        with self.transaction():
            old = self.db.execute("SELECT id,status FROM jobs WHERE source_id=? AND article_key=?", (source_id, key)).fetchone()
            if not old:
                old = self.db.execute("SELECT jobs.id,jobs.status FROM aliases JOIN jobs ON jobs.id=aliases.job_id WHERE aliases.source_id=? AND aliases.alias=?",
                                      (source_id, key)).fetchone()
            if old:
                if old["status"] == "BASELINE":
                    self._update(old["id"], status="DISCOVERED", reason="Historical article explicitly imported", retry_at=None)
                    self.event("baseline_activated_by_import", old["id"], imported_article_key=key)
                self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?)", (source_id, key, old["id"]))
                return old[0], False
            job_id = uuid.uuid4().hex
            at = now_iso()
            self.db.execute("INSERT INTO jobs(id,source_id,article_key,status,article,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (job_id, source_id, key, "DISCOVERED", json.dumps(article, ensure_ascii=False), at, at))
            self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?)", (source_id, key, job_id))
            self.event("explicit_import", job_id)
            return job_id, True

    def _record_feed_snapshot(self, source_id, key, article):
        """Compare feed evidence with feed evidence, never with acquired text.

        Legacy/explicitly imported jobs have no feed snapshot; their first poll
        establishes a baseline. Retrieval times and transport fields do not
        influence the signature. This runs inside discover's transaction.
        """
        payload = {field: article.get(field) or "" for field in (
            "title", "author", "content", "published_at", "modified_at", "updated_at", "date_modified",
        )}
        signature = digest(payload)
        old = self.db.execute("SELECT signature FROM feed_snapshots WHERE source_id=? AND article_key=?",
                              (source_id, key)).fetchone()
        self.db.execute("INSERT INTO feed_snapshots VALUES(?,?,?,?,?) ON CONFLICT(source_id,article_key) DO UPDATE SET signature=excluded.signature,payload=excluded.payload,observed_at=excluded.observed_at",
                        (source_id, key, signature, json.dumps(payload, ensure_ascii=False), now_iso()))
        return old[0] if old else None, signature

    def discover(self, source, result, baseline=False):
        added, changed = [], []
        with self.transaction():
            for article in result["articles"]:
                article = dict(article, source_id=source["id"])
                key = article.get("article_id") or article.get("url")
                if not key:
                    raise StateError("Discovered article has no stable identity")
                previous_feed_hash, feed_hash = self._record_feed_snapshot(source["id"], key, article)
                old = self.db.execute("SELECT * FROM jobs WHERE source_id=? AND article_key=?",
                                      (source["id"], key)).fetchone()
                if not old:
                    alias = self.db.execute("SELECT job_id FROM aliases WHERE source_id=? AND alias=?", (source["id"], key)).fetchone()
                    if alias:
                        old = self.db.execute("SELECT * FROM jobs WHERE id=?", (alias[0],)).fetchone()
                if old:
                    if old["status"] in ("BASELINE", "DUPLICATE", "SAMPLE_READY"):
                        continue
                    if previous_feed_hash is not None and previous_feed_hash != feed_hash:
                        if self.db.execute("SELECT 1 FROM attempts WHERE job_id=?", (old["id"],)).fetchone():
                            self.event("source_changed_after_intent", old["id"], article_key=key,
                                       previous_feed_hash=previous_feed_hash, feed_hash=feed_hash)
                        else:
                            # A feed change is a signal, not proof of a new full
                            # source version. Preserve review/materials until a
                            # fresh acquire compares actual article evidence.
                            self._update(old["id"], status="SOURCE_RECHECK", retry_at=None,
                                         reason="Feed changed; verify actual source before continuing")
                            self.event("source_recheck_requested", old["id"], previous_status=old["status"],
                                       article_key=key, previous_feed_hash=previous_feed_hash, feed_hash=feed_hash)
                        changed.append(old["id"])
                    continue
                job_id = uuid.uuid4().hex
                at = now_iso()
                status = "BASELINE" if baseline else "DISCOVERED"
                self.db.execute("INSERT INTO jobs(id,source_id,article_key,status,article,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                (job_id, source["id"], key, status, json.dumps(article, ensure_ascii=False), at, at))
                self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?)", (source["id"], key, job_id))
                added.append(job_id)
                self.event("discovered", job_id, baseline=baseline)
            self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)",
                            (source["id"], json.dumps(result.get("cursor", {})), "healthy", now_iso(), None))
        return {"added": added, "changed": changed, "baseline": baseline}

    def source_failure(self, source_id, reason):
        self.db.execute("INSERT INTO sources(id,health,error) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET health=excluded.health,error=excluded.error",
                        (source_id, "error", reason))
        self.event("source_error", source_id=source_id, reason=reason)

    def get_job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise StateError("Unknown job id")
        data = dict(row)
        for key in ("article", "draft", "review", "artifacts"):
            data[key] = json.loads(data[key]) if data[key] else None
        outline = self.db.execute("SELECT * FROM source_outlines WHERE job_id=?", (job_id,)).fetchone()
        data["source_outline"] = json.loads(outline["payload"]) if outline else None
        data["source_outline_hash"] = outline["outline_hash"] if outline else None
        return data

    def jobs(self, statuses=None):
        query, params = "SELECT id FROM jobs", []
        if statuses:
            query += " WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
            params = list(statuses)
        return [self.get_job(row[0]) for row in self.db.execute(query + " ORDER BY created_at,id", params)]

    def _update(self, job_id, **fields):
        allowed = {"status", "article", "source_hash", "draft", "content_hash", "review", "artifacts", "revision", "reason", "retry_at"}
        if not fields or set(fields) - allowed:
            raise StateError("Invalid job update")
        fields["updated_at"] = now_iso()
        values = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", values + [job_id])

    def update(self, job_id, expected_content_hash=None, **fields):
        with self.transaction():
            if self.db.execute("SELECT 1 FROM attempts WHERE job_id=?", (job_id,)).fetchone():
                raise StateError("Job has a publication intent; content is frozen")
            current = self.get_job(job_id)
            if expected_content_hash is not None and current["content_hash"] != expected_content_hash:
                raise StateError("Content changed while this step was running")
            self._update(job_id, **fields)
            if "source_hash" in fields and fields["source_hash"] != current["source_hash"]:
                self.db.execute("DELETE FROM source_outlines WHERE job_id=?", (job_id,))
            article = fields.get("article")
            if article and article.get("canonical_article_id"):
                alias = self.db.execute("SELECT job_id FROM aliases WHERE source_id=? AND alias=?", (current["source_id"], article["canonical_article_id"])).fetchone()
                if alias and alias[0] != job_id:
                    self._update(job_id, status="DUPLICATE", reason="Canonical article already tracked")
                    self.event("canonical_duplicate", job_id, existing_job_id=alias[0])
                else:
                    self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?)", (current["source_id"], article["canonical_article_id"], job_id))

    def get_attempt(self, attempt_id):
        row = self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if not row:
            raise StateError("Unknown publication attempt")
        data = dict(row)
        data["observation"] = json.loads(data["observation"]) if data["observation"] else None
        return data

    def prepare_attempt(self, job_id, account_id, policy_hash, evidence, max_per_day, today, expected_content_hash=None):
        with self.transaction():
            if self.paused():
                raise StateError("Automation paused")
            job = self.get_job(job_id)
            if expected_content_hash is not None and job['content_hash'] != expected_content_hash:
                raise StateError('Content changed before submission intent; obtain a new review')
            if job["status"] != "READY":
                raise StateError("Only READY jobs may be submitted")
            if self.db.execute("SELECT 1 FROM attempts WHERE job_id=?", (job_id,)).fetchone():
                raise StateError("A submission intent already exists; reconcile instead of resubmitting")
            if self.db.execute("SELECT 1 FROM account_locks WHERE account_id=?", (account_id,)).fetchone():
                raise StateError("Account has an unresolved submission; reconcile it first")
            used = self.db.execute("SELECT COUNT(*) FROM attempts WHERE account_id=? AND substr(intent_at,1,10)=?",
                                   (account_id, today)).fetchone()[0]
            if used >= max_per_day:
                raise StateError("Daily publication limit reached")
            attempt_id = uuid.uuid4().hex
            at = now_iso()
            self.db.execute("INSERT INTO attempts(attempt_id,job_id,account_id,title,content_hash,policy_hash,status,intent_at,submitted_at,account_evidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (attempt_id, job_id, account_id, job["draft"]["title"], job["content_hash"], policy_hash,
                             "SUBMIT_INTENT", at, at, str(evidence)))
            self.db.execute("INSERT INTO account_locks VALUES(?,?)", (account_id, attempt_id))
            self._update(job_id, status="SUBMIT_INTENT")
            self.event("submit_intent", job_id, attempt_id=attempt_id, account_id=account_id)
        return self.get_attempt(attempt_id)

    def record_submit(self, attempt_id):
        with self.transaction():
            attempt = self.get_attempt(attempt_id)
            if attempt["status"] != "SUBMIT_INTENT":
                raise StateError("Submit already recorded or resolved; do not click again")
            # Keep the earliest intent boundary for excluding older platform records.
            # The click may precede this local acknowledgement by several seconds.
            self.db.execute("UPDATE attempts SET status='SUBMITTED' WHERE attempt_id=?", (attempt_id,))
            self._update(attempt["job_id"], status="SUBMITTED")
            self.event("submitted", attempt["job_id"], attempt_id=attempt_id, recorded_at=now_iso())
        return self.get_attempt(attempt_id)

    def reconcile(self, attempt_id, result, observation):
        with self.transaction():
            attempt = self.get_attempt(attempt_id)
            old = attempt["status"]
            if old in ("PUBLISHED", "REJECTED"):
                raise StateError("Terminal publication result is immutable")
            if old in ("PENDING_REVIEW", "SCHEDULED") and result.get("confirmed") is not True:
                # A failed follow-up query cannot invalidate a prior matching
                # platform record. Retain its state and evidence for recovery.
                self.event("publication_recheck_failed", attempt["job_id"], attempt_id=attempt_id,
                           last_trusted_status=old, reason=result.get("reason"),
                           error_code=result.get("error_code"), observation=observation)
                return self.get_attempt(attempt_id)
            status = result["status"].upper()
            self.db.execute("UPDATE attempts SET status=?,observation=? WHERE attempt_id=?",
                            (status, json.dumps(observation, ensure_ascii=False), attempt_id))
            self._update(attempt["job_id"], status=status, reason=result.get("reason"))
            if result.get("confirmed") is True:
                self.db.execute("DELETE FROM account_locks WHERE attempt_id=?", (attempt_id,))
            self.event("publication_observed", attempt["job_id"], attempt_id=attempt_id, status=status)
        return self.get_attempt(attempt_id)

    def recover_intents(self):
        # An old intent may have been clicked before a crash. Never release it automatically.
        with self.transaction():
            rows = self.db.execute("SELECT attempt_id,job_id FROM attempts WHERE status='SUBMIT_INTENT'").fetchall()
            for row in rows:
                self.db.execute("UPDATE attempts SET status='SUBMIT_UNKNOWN' WHERE attempt_id=?", (row[0],))
                self._update(row[1], status="SUBMIT_UNKNOWN", reason="Interrupted intent; read-only reconciliation required")
            return len(rows)

    def manual_retry_parents(self):
        """Keep explicitly superseded unknown attempts in the audit ledger.

        A bound human retry transfers active reconciliation to its child; it
        does not assert that the original submission failed or was published.
        Ordinary unknown attempts remain active without this explicit event.
        """
        parents = set()
        for row in self.db.execute("SELECT details FROM events WHERE kind='human_authorized_retry_intent'"):
            try:
                data = json.loads(row[0])
                parent = self.get_attempt(data['parent_attempt_id'])
                child = self.get_attempt(data['child_attempt_id'])
                if (data.get('human_authorized') is True and data.get('max_extra_submissions') == 1
                        and parent['status'] == 'SUBMIT_UNKNOWN' and parent['job_id'] != child['job_id']
                        and parent['content_hash'] == child['content_hash'] == data.get('content_hash')
                        and parent['account_id'] == child['account_id'] == data.get('account_id')
                        and Path(data['authorization_ref']).is_file()
                        and hashlib.sha256(Path(data['authorization_ref']).read_bytes()).hexdigest() == data.get('authorization_sha256')):
                    parents.add(parent['attempt_id'])
            except (KeyError, ValueError, TypeError, OSError):
                continue
        return parents

    def status(self):
        return {"paused": self.paused(), "database": str(self.path),
                "jobs": dict(self.db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall()),
                "sources": [dict(r) for r in self.db.execute("SELECT * FROM sources")],
                "attempts": [dict(r) for r in self.db.execute("SELECT attempt_id,job_id,account_id,status,intent_at FROM attempts ORDER BY intent_at")],
                "run_leases": [dict(r) for r in self.db.execute("SELECT name,owner,expires_at FROM run_leases")],
                "account_locks": [dict(r) for r in self.db.execute("SELECT * FROM account_locks")]}

    def backup(self, destination):
        target = Path(destination)
        if target.exists():
            raise StateError("Backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_db = sqlite3.connect(str(target))
        try:
            self.db.backup(backup_db)
        finally:
            backup_db.close()
        return str(target)
