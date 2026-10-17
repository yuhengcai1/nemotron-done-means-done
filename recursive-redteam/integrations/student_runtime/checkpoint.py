"""Durable model-call replay and bounded transport backoff.

The caller owns student decisions, target tools and source identity. Replaying a
whole episode is safe only for a deterministic fresh-state local suite. This is
not an exactly-once executor for external side effects. Keep one database for
all roles sharing a run budget; use a unique episode ID for each independent
sample. Cached completions are continuation evidence, never new model samples.
"""
from dataclasses import asdict
import json
import re
from pathlib import Path
import sqlite3
import time

from recursive_redteam.contracts import Completion, ModelError, ProviderError
from recursive_redteam.teaching import canonical, digest


class Deferred(RuntimeError):
    def __init__(self, reason, retry_at=None):
        self.reason, self.retry_at = reason, retry_at
        super().__init__(reason)


class CheckpointLedger:
    def __init__(self, path, *, max_attempts=12, per_launch=3,
                 min_interval=15, error_cooldown=60, deadline=None, clock=time.time):
        for value in (max_attempts, per_launch):
            if type(value) is not int or value < 1:
                raise ValueError('attempt limits must be positive integers')
        for value in (min_interval, error_cooldown):
            if type(value) not in (int, float) or not 0 <= value <= 3600:
                raise ValueError('invalid interval')
        self.clock, self.per_launch, self.started = clock, per_launch, 0
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY, body TEXT NOT NULL, next_allowed REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY, identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, episode TEXT NOT NULL,
            role TEXT NOT NULL, position INTEGER NOT NULL, request_hash TEXT NOT NULL,
            request TEXT NOT NULL, status TEXT NOT NULL, response TEXT, error TEXT,
            started REAL NOT NULL, finished REAL);
        ''')
        self.policy = dict(max_attempts=max_attempts, min_interval=min_interval, error_cooldown=error_cooldown)
        if deadline is not None:
            if type(deadline) not in (int,float) or not 0<deadline<float('inf'):
                raise ValueError('invalid persistent deadline')
            self.policy['deadline']=deadline
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT body FROM policy WHERE id=1').fetchone()
            if row and json.loads(row[0]) != self.policy:
                raise ValueError('saved budget/pacing policy differs; do not silently reset it')
            self.db.execute('INSERT OR IGNORE INTO policy VALUES(1,?,0)', (canonical(self.policy),))

    def close(self):
        self.db.close()

    def model(self, model, *, episode, role, identity):
        if not isinstance(episode, str) or not episode or not isinstance(role, str) or not role:
            raise ValueError('episode and role must be nonempty')
        if getattr(model, 'max_retries', None) != 0:
            raise ValueError('adapter must declare max_retries=0; hidden retries bypass attempt accounting')
        if not isinstance(identity, dict) or not identity:
            raise ValueError('source/model/lesson identity is required')
        body = canonical(identity)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT identity FROM episodes WHERE id=?', (episode,)).fetchone()
            if row and row[0] != body:
                raise ValueError('episode identity changed; cached answers cannot cross versions or conditions')
            self.db.execute('INSERT OR IGNORE INTO episodes VALUES(?,?)', (episode, body))
        return CheckpointModel(self, model, episode, role)

    def report(self):
        counts = dict(self.db.execute('SELECT status,COUNT(*) FROM calls GROUP BY status'))
        return {'attempts': sum(counts.values()), 'counts': counts, 'maximum': self.policy['max_attempts'],
                'remaining': max(0, self.policy['max_attempts']-sum(counts.values())),
                'next_allowed': self.db.execute('SELECT next_allowed FROM policy').fetchone()[0],
                'unknown_outcomes': counts.get('pending', 0)+counts.get('unknown', 0),
                'meaning': 'Attempts include errors and unknown outcomes. Replayed completions are not fresh inference.'}

    def journal(self):
        """Export protocol receipts only; credentials never enter this ledger."""
        calls=[]; usage={}
        for row in self.db.execute('SELECT * FROM calls ORDER BY id'):
            call=dict(row)
            call['request']=json.loads(call['request'])
            call['response']=json.loads(call['response']) if call['response'] else None
            call['usage']=(call['response'] or {}).get('usage',{})
            call['actual_model']=(call['response'] or {}).get('model')
            if call['error'] and call['response'] is None:
                call['response']={'diagnostic':call['error']}
            for key,value in call['usage'].items():
                if type(value) is int and value>=0:usage[key]=usage.get(key,0)+value
            calls.append(call)
        return {'model_calls':calls, 'model_usage':{'logical_calls':len(calls),
                'actual_http_attempts':len(calls), 'reported_tokens':usage,
                'unknown_usage_calls':sum(not c['usage'] for c in calls)},
                'budget':self.report(), 'transport_assumption':'Adapter max_retries=0; one complete() call per reserved attempt.'}


class CheckpointModel:
    def __init__(self, ledger, model, episode, role):
        self.ledger, self.model, self.episode, self.role = ledger, model, episode, role
        self.position = 0
        self.replayed = 0

    def complete(self, messages, *, tools=None, json_mode=False):
        ledger = self.ledger
        request = {'messages': messages, 'tools': tools, 'json_mode': json_mode}
        serialized, key = canonical(request), digest(request)
        now = ledger.clock()
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            rows = ledger.db.execute('SELECT * FROM calls WHERE episode=? AND role=? AND position=? ORDER BY id',
                                     (self.episode, self.role, self.position)).fetchall()
            if any(row['request_hash'] != key or row['request'] != serialized for row in rows):
                raise ValueError('resume transcript diverged at a saved model call')
            if rows and rows[-1]['status'] == 'done':
                completion = Completion(**json.loads(rows[-1]['response']))
                self.position += 1; self.replayed += 1
                return completion
            # An interrupted request may already have been billed/executed. Do
            # not auto-repeat it, even if its process or lease disappeared.
            if ledger.db.execute("SELECT 1 FROM calls WHERE status IN ('pending','unknown') LIMIT 1").fetchone():
                raise Deferred('unknown_or_inflight_request_requires_reconciliation')
            if rows and rows[-1]['status']=='rejected':
                raise Deferred('provider_rejected_request_requires_configuration_fix')
            if now>=ledger.policy.get('deadline',float('inf')):
                raise Deferred('campaign_wall_budget_exhausted')
            attempts = ledger.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
            if attempts >= ledger.policy['max_attempts']:
                raise Deferred('persistent_attempt_budget_exhausted')
            if ledger.started >= ledger.per_launch:
                raise Deferred('per_launch_attempt_budget_exhausted')
            next_allowed = ledger.db.execute('SELECT next_allowed FROM policy').fetchone()[0]
            if now < next_allowed:
                raise Deferred('transport_cooldown', next_allowed)
            cursor = ledger.db.execute('''INSERT INTO calls(episode,role,position,request_hash,request,status,started)
                VALUES(?,?,?,?,?,'pending',?)''', (self.episode,self.role,self.position,key,serialized,now))
            call_id = cursor.lastrowid
            ledger.db.execute('UPDATE policy SET next_allowed=? WHERE id=1', (now+ledger.policy['min_interval'],))
        ledger.started += 1
        try:
            completion = self.model.complete(messages, tools=tools, json_mode=json_mode)
            if not isinstance(completion, Completion):
                raise ModelError('invalid completion envelope')
            response = canonical(asdict(completion))
        except ModelError as exc:
            # ProviderError diagnostics are deliberately scrubbed by the trusted
            # adapter; never serialize arbitrary exception messages or keys.
            diagnostic = exc.diagnostic if isinstance(exc, ProviderError) else type(exc).__name__
            match = re.fullmatch(r'Provider HTTP (\d{3}) after 1 attempt\(s\)', diagnostic)
            status = int(match[1]) if match else None
            # Only explicit throttling is automatically retryable. Timeouts,
            # broken response envelopes and server errors may follow execution.
            state = 'error' if status==429 else 'rejected' if status in (400,401,403,404,422) else 'unknown'
            until = ledger.clock()+ledger.policy['error_cooldown']
            with ledger.db:
                ledger.db.execute('UPDATE calls SET status=?,error=?,finished=? WHERE id=?',
                                  (state,diagnostic,ledger.clock(),call_id))
                ledger.db.execute('UPDATE policy SET next_allowed=MAX(next_allowed,?) WHERE id=1', (until,))
            reason = 'rate_limit_checkpointed' if state=='error' else 'unknown_request_requires_reconciliation' if state=='unknown' else 'provider_rejected_request_requires_configuration_fix'
            raise Deferred(reason, until if state=='error' else None) from None
        # Crashes/KeyboardInterrupt leave the pending reservation visible. A
        # later invocation must not guess whether the API completed the request.
        with ledger.db:
            ledger.db.execute("UPDATE calls SET status='done',response=?,finished=? WHERE id=?",
                              (response,ledger.clock(),call_id))
        self.position += 1
        return completion
