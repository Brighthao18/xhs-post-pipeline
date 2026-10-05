"""Bind a reviewed backend editor session to the durable local attempt ledger."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid
from urllib.parse import urlsplit

from .backend import BackendClient, BackendError
from .config import TZ, digest, load_config, now_iso, read_json, write_json
from .quality import verify_artifacts, verify_review
from .store import StateError


class BackendWorkflow:
    def _backend(self):
        return BackendClient(self.config.get('publication_backend', {}))

    def _backend_tables(self):
        self.store.db.executescript('''
        CREATE TABLE IF NOT EXISTS backend_sessions(
          job_id TEXT PRIMARY KEY REFERENCES jobs(id), content_hash TEXT NOT NULL,
          session_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
          response TEXT NOT NULL, review TEXT);
        CREATE TABLE IF NOT EXISTS backend_calls(
          attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
          payload_hash TEXT NOT NULL, started_at TEXT NOT NULL, result TEXT);
        ''')

    @staticmethod
    def _fresh(timestamp, seconds=900):
        try:
            value = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            raise StateError('Backend evidence has no valid timestamp') from None
        if value.tzinfo is None or not 0 <= (datetime.now(TZ) - value).total_seconds() <= seconds:
            raise StateError('Backend identity/editor evidence is not fresh')

    def _actual_file(self, path, suffix='.png'):
        if not isinstance(path, str) or not path:
            raise StateError('Backend returned no actual local evidence file')
        value = Path(path)
        root = Path(self.config['publication_backend']['auth_token_path']).parent / 'session' / 'evidence'
        if (not root.is_absolute() or not value.is_absolute()
                or not value.is_relative_to(root) or value.suffix.lower() != suffix
                or any(':' in part for part in value.parts[1:])):
            raise StateError('Backend evidence is outside its private capture directory')
        if root.resolve() != root or not value.resolve().is_relative_to(root) or not value.is_file():
            raise StateError('Backend returned no actual local evidence file')
        return value

    @staticmethod
    def _compact_editor(text):
        if not isinstance(text, str):
            return None
        return ''.join(c for c in text if not c.isspace() and c not in '\u200b\u200c\u200d\ufeff')

    def _verify_editor_fields(self, data, payload, *, record=False):
        expected = payload['content'] + ''.join('#' + t.lstrip('#') for t in payload['tags'])
        if (not isinstance(data, dict) or not isinstance(data.get('title'), str)
                or data['title'].strip() != payload['title'].strip()
                or self._compact_editor(data.get('content')) != self._compact_editor(expected)
                or data.get('ai_declared') is not True):
            raise StateError('Backend editor readback does not match the reviewed content/declaration')
        if record:
            previews = data.get('preview_srcs')
            if (not isinstance(previews, list) or len(previews) != len(payload['images'])
                    or any(not isinstance(p, str) or not p.strip() or p.strip().lower() == 'none' for p in previews)):
                raise StateError('Backend record has incomplete actual image previews')
        elif (data.get('prepared') is not True or data.get('tags') != payload['tags']
              or type(data.get('image_count')) is not int or data['image_count'] != len(payload['images'])
              or data.get('upload_order_verified') is not True):
            raise StateError('Backend prepared fields do not match the reviewed material')

    def _verify_image_evidence(self, data, image_count):
        paths, hashes = data.get('image_evidence_refs'), data.get('image_evidence_sha256')
        if (not isinstance(paths, list) or not isinstance(hashes, list)
                or len(paths) != image_count or len(hashes) != image_count
                or len(set(p for p in paths if isinstance(p, str))) != image_count):
            raise StateError('Actual per-image backend captures are incomplete')
        for path, expected in zip(paths, hashes):
            evidence = self._actual_file(path)
            if hashlib.sha256(evidence.read_bytes()).hexdigest() != expected:
                raise StateError('Actual per-image backend capture changed or has the wrong hash')

    def _check_identity(self, identity, require_user=True):
        expected = self.config['account']
        if (not isinstance(identity, dict) or identity.get('authenticated') is not True
                or identity.get('red_id') != expected['account_id']
                or identity.get('nickname') != expected['nickname']
                or not identity.get('user_id')):
            raise StateError('Backend authenticated account does not match the configured target')
        if require_user and identity['user_id'] != expected.get('user_id'):
            raise StateError('Backend user_id is unbound or changed; bind the observed target first')
        self._fresh(identity.get('observed_at'))
        self._actual_file(identity.get('evidence_ref'))
        return identity

    def backend_health(self):
        return self._backend().health()

    def backend_management(self, view='all'):
        """Obtain actual creator records without concluding or submitting a post."""
        self.check_run()
        labels = {'all': '', 'published': '已发布', 'pending_review': '审核中', 'rejected': '未通过'}
        if not isinstance(view, str) or view not in labels:
            raise StateError('Unsupported read-only management view')
        data = self._backend().management_evidence(view=view)['data']
        if (not isinstance(data, dict) or data.get('phase') != 'READ_ONLY' or data.get('unverified') is not True
                or data.get('confirmed') is not False or data.get('published') is not False
                or data.get('view') != view or data.get('filter_label') != labels[view]):
            raise StateError('Management evidence cannot itself confirm a publication')
        self._check_identity(data.get('account'))
        self._fresh(data.get('observed_at'))
        try:
            url = urlsplit(data.get('url'))
            if (url.scheme != 'https' or url.hostname != 'creator.xiaohongshu.com'
                    or url.port not in (None, 443) or url.username or url.password or url.query or url.fragment):
                raise ValueError('url')
        except (TypeError, ValueError):
            raise StateError('Management capture does not identify the actual creator platform') from None
        capture = self._actual_file(data.get('evidence_ref'))
        if hashlib.sha256(capture.read_bytes()).hexdigest() != data.get('evidence_sha256'):
            raise StateError('Management screenshot disagrees with its actual file')
        record_path = self._actual_file(data.get('record_ref'), '.json')
        try:
            record = read_json(record_path)
        except (OSError, ValueError):
            raise StateError('Management page record cannot be read as JSON') from None
        if (not isinstance(record, dict) or record.get('url') != data.get('url')
                or record.get('view') != view or record.get('filter_label') != labels[view]
                or not isinstance(record.get('visible_text'), str) or not record['visible_text'].strip()):
            raise StateError('Management page record disagrees with the actual capture')
        self._check_identity(record.get('account'))
        return {'management': data,
                'instruction': 'Actually view the management PNG and read its complete record, including the requested filter and visible selection. Match the unique note, explicit platform state and time with detail/body/ordered image evidence before reconcile. A requested/clicked filter and this read-only capture are not publication confirmation.'}

    def backend_identity(self, bind=False):
        if bind:
            self.check_run()
        identity = self._backend().identity()['data']
        self._check_identity(identity, require_user=not bind)
        if bind:
            data = read_json(self.config['config_path'])
            data['account'].update(user_id=identity['user_id'], backend_bound_at=now_iso(),
                                   backend_binding_evidence=identity['evidence_ref'])
            write_json(self.config['config_path'], data)
            self.config = load_config(self.config['config_path'])
            self.store.event('backend_account_bound', user_id=identity['user_id'], red_id=identity['red_id'])
        return {'identity': identity, 'bound': bind}

    def _backend_payload(self, job):
        self.check_public_copy(job['draft'])
        account = self.config['account']
        if not account.get('user_id'):
            raise StateError('The authenticated backend user_id must be observed and bound')
        health = self.source(job['source_id']).get('domain') == 'health'
        if not job['review'] or not self._review_valid(job, job['review'], health=health, final=True):
            raise StateError('A current final content/material review is required')
        files = self._verify_materials(job)
        if job['review'].get('manifest_hash') != files['manifest_hash']:
            raise StateError('Materials changed after their final review')
        return dict(content_hash=job['content_hash'],
                    expected_account=dict(user_id=account['user_id'], red_id=account['account_id'], nickname=account['nickname']),
                    title=job['draft']['title'], content=job['draft']['body'], tags=job['draft']['tags'],
                    images=[f['path'] for f in files['publish_images']], image_hashes=[f['sha256'] for f in files['publish_images']],
                    ai_generated=True)

    def backend_preflight(self, job_id):
        self.check_run(required=True)
        if self.store.paused():
            raise StateError('Automation paused')
        job = self.store.get_job(job_id)
        if job['status'] not in ('READY', 'SAMPLE_READY'):
            raise StateError('Only reviewed ready material may enter backend preflight')
        client = self._backend()
        self._check_identity(client.identity()['data'])
        payload = self._backend_payload(job)
        payload['attempt_id'] = 'preflight_' + uuid.uuid4().hex
        data = client.preflight(payload)['data']
        if (not isinstance(data, dict) or data.get('phase') != 'PREPARED'
                or data.get('content_hash') != job['content_hash'] or not data.get('editor_session_id')):
            raise StateError('Backend did not preserve a prepared editor session')
        self._verify_editor_fields(data, payload)
        identity = data.get('account') or data.get('identity') or {}
        self._check_identity(identity)
        self._fresh(data.get('observed_at', identity['observed_at']))
        try:
            expiration = datetime.fromisoformat(data.get('expires_at'))
        except (TypeError, ValueError):
            raise StateError('Prepared editor session has no valid expiry') from None
        if expiration.tzinfo is None or expiration <= datetime.now(TZ):
            raise StateError('Prepared editor session already expired')
        evidence = self._actual_file(data.get('evidence_ref'))
        record_path = self._actual_file(data.get('record_ref'), '.json')
        try:
            record = read_json(record_path)
        except (OSError, ValueError):
            raise StateError('Backend page record cannot be read as JSON') from None
        if (not isinstance(record, dict) or record.get('content_hash') != job['content_hash']
                or record.get('editor_session_id') != data['editor_session_id']
                or record.get('image_hashes') != payload['image_hashes']
                or record.get('expected_paths') != payload['images']):
            raise StateError('Backend page record does not bind the prepared content and ordered files')
        self._check_identity(record.get('account', {}))
        self._verify_editor_fields(record.get('editor'), payload, record=True)
        if (record.get('image_evidence_refs') != data.get('image_evidence_refs')
                or record.get('image_evidence_sha256') != data.get('image_evidence_sha256')):
            raise StateError('Page record disagrees with the ordered image captures')
        self._verify_image_evidence(data, len(payload['images']))
        self._backend_tables()
        evidence_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
        if data.get('evidence_sha256') != evidence_hash:
            raise StateError('Backend screenshot hash disagrees with the actual capture')
        data['local_record_hash'] = hashlib.sha256(record_path.read_bytes()).hexdigest()
        data['local_policy_hash'] = self.config['policy_hash']
        with self.store.transaction():
            if self.store.get_job(job_id)['content_hash'] != job['content_hash']:
                raise StateError('Content changed during backend preflight')
            self.store.db.execute('INSERT OR REPLACE INTO backend_sessions VALUES(?,?,?,?,?,NULL)',
                                  (job_id, job['content_hash'], data['editor_session_id'], evidence_hash, json.dumps(data, ensure_ascii=False)))
            self.store.event('backend_prepared', job_id, session_id=data['editor_session_id'], evidence_ref=str(evidence))
        return {'preflight': data, 'evidence_hash': evidence_hash,
                'instruction': 'Actually view the editor capture and every image_evidence_refs capture, then read the complete page record including raw_content. Review title, body, all images/order, account and AI declaration. This is not a submission.'}

    def _prepared_session(self, job_id):
        self._backend_tables()
        row = self.store.db.execute('SELECT * FROM backend_sessions WHERE job_id=?', (job_id,)).fetchone()
        if not row:
            raise StateError('No prepared backend editor session exists')
        session = dict(row)
        session['response'] = json.loads(session['response'])
        session['review'] = json.loads(session['review']) if session['review'] else None
        job = self.store.get_job(job_id)
        if session['content_hash'] != job['content_hash']:
            raise StateError('Prepared editor session belongs to an obsolete content version')
        if session['response'].get('local_policy_hash') != self.config['policy_hash']:
            raise StateError('Prepared editor session belongs to an obsolete policy/account binding')
        expiration = datetime.fromisoformat(session['response']['expires_at'])
        if expiration.tzinfo is None or expiration <= datetime.now(TZ):
            raise StateError('Prepared editor session expired; obtain and review a fresh preflight')
        evidence = self._actual_file(session['response']['evidence_ref'])
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != session['evidence_hash']:
            raise StateError('Prepared editor evidence changed after capture')
        record = self._actual_file(session['response']['record_ref'], '.json')
        if hashlib.sha256(record.read_bytes()).hexdigest() != session['response'].get('local_record_hash'):
            raise StateError('Prepared page record changed after capture')
        files = self._verify_materials(job)
        self._verify_image_evidence(session['response'], len(files['publish_images']))
        return session

    def backend_review(self, job_id, data):
        self.check_run(required=True)
        session = self._prepared_session(job_id)
        required = ('account', 'title', 'body', 'images', 'image_order', 'ai_declaration')
        if (data.get('job_id') != job_id or data.get('content_hash') != session['content_hash']
                or data.get('editor_session_id') != session['session_id']
                or data.get('evidence_hash') != session['evidence_hash']
                or data.get('evidence_ref') != session['response']['evidence_ref']
                or data.get('passed') is not True or data.get('issues') != []
                or any(data.get('checks', {}).get(k) is not True for k in required)):
            raise StateError('Backend visual review must bind the actual editor/session/evidence and pass all checks')
        self.store.db.execute('UPDATE backend_sessions SET review=? WHERE job_id=? AND session_id=?',
                              (json.dumps(data, ensure_ascii=False), job_id, session['session_id']))
        self.store.event('backend_visual_reviewed', job_id, session_id=session['session_id'])
        return {'reviewed': True, 'editor_session_id': session['session_id']}

    def backend_submit(self, job_id):
        self.check_run(required=True)
        self._backend_tables()
        if self.store.db.execute('SELECT 1 FROM attempts WHERE job_id=?', (job_id,)).fetchone():
            raise StateError('A submission intent already exists; use observations/reconcile, never resubmit')
        job = self.store.get_job(job_id)
        if job['status'] != 'READY':
            raise StateError('Only active READY jobs may submit; initial/sample history is excluded')
        session = self._prepared_session(job_id)
        if not session['review']:
            raise StateError('Actual editor evidence has not received a bound visual review')
        client = self._backend()
        identity = self._check_identity(client.identity()['data'])
        account_path = Path(self.config['work_dir']) / job_id / 'backend-account-evidence.json'
        write_json(account_path, dict(account_id=identity['red_id'], nickname=identity['nickname'],
                                      authenticated=True, observed_at=identity['observed_at'], evidence_ref=identity['evidence_ref']))
        # Identity readback can take time; revalidate the retained session and
        # require the ledger to freeze exactly the reviewed content version.
        session = self._prepared_session(job_id)
        if session['content_hash'] != job['content_hash'] or not session['review']:
            raise StateError('Prepared content changed during account verification')
        payload = self._backend_payload(job)
        prepared = self.prepare_publish(job_id, identity['red_id'], account_path,
                                        expected_content_hash=session['content_hash'])
        attempt = prepared['attempt']
        payload.update(attempt_id=attempt['attempt_id'], editor_session_id=session['session_id'],
                       visual_reviewed=True, reviewed_evidence_ref=session['response']['evidence_ref'])
        # This marker is saved before HTTP. An interruption here or later can
        # never cause a future run to repeat the submission call.
        with self.store.transaction():
            self.store.db.execute('INSERT INTO backend_calls VALUES(?,?,?,NULL)',
                                  (attempt['attempt_id'], digest(payload), now_iso()))
        try:
            result = client.submit(payload)['data']
        except BackendError as error:
            result = dict(phase=error.phase or 'SUBMIT_UNKNOWN', error_code=error.code,
                          retry_allowed=False, published=False, confirmed=False,
                          backend_code=error.backend_code, evidence_ref=error.evidence_ref,
                          record_ref=error.record_ref)
        except Exception:
            result = dict(phase='SUBMIT_UNKNOWN', error_code='local_dispatch_error',
                          retry_allowed=False, published=False, confirmed=False)
        self.store.db.execute('UPDATE backend_calls SET result=? WHERE attempt_id=?',
                              (json.dumps(result, ensure_ascii=False), attempt['attempt_id']))
        if result.get('click_attempted') is True:
            self.store.record_submit(attempt['attempt_id'])
        # No dispatch result is a platform confirmation, including known
        # pre-click failure. Preserve the intent and reconcile it explicitly.
        observed = dict(status='submit_unknown', confirmed=False, published=False,
                        retry_allowed=False, reason='Backend dispatch outcome requires actual platform reconciliation')
        saved = self.store.reconcile(attempt['attempt_id'], observed, {'backend_dispatch': result})
        return {'attempt': saved, 'dispatch': result, 'published': False,
                'instruction': 'Use actual management/detail records for reconciliation; do not resubmit this job.'}

    def backend_observations(self, attempt_id):
        self.store.get_attempt(attempt_id)
        return self._backend().observations(attempt_id)
