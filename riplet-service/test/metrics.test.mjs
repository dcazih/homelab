import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createMetrics } from '../service/metrics.mjs';
import { validateOptions } from '../service/validation.mjs';

test('metrics survive reopening and distinguish conversion from transfer', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'riplet-metrics-test-'));
  const file = path.join(dir, 'metrics.sqlite');
  try {
    let metrics = createMetrics(file);
    metrics.record('job_started', { format: 'mp3', quality: '192' });
    metrics.record('job_completed', { format: 'mp3', quality: '192', bytes: 1000, durationMs: 500 });
    metrics.record('download_interrupted', { format: 'mp3', quality: '192', delivered: 300 });
    metrics.close();
    metrics = createMetrics(file);
    metrics.record('download_completed', { format: 'mp3', quality: '192', delivered: 1000 });
    const rows = Object.fromEntries(metrics.snapshot().counts.map(row => [row.kind, row]));
    assert.equal(rows.job_completed.count, 1);
    assert.equal(rows.download_completed.count, 1);
    assert.equal(rows.download_interrupted.delivered, 300);
    assert.equal(rows.download_completed.delivered, 1000);
    metrics.close();
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('free and supporter quality gates match displayed choices', () => {
  const url = 'https://www.youtube.com/watch?v=MtZpcG30n6s';
  assert.equal(validateOptions({ url, format: 'mp3', quality: '192' }).quality, '192');
  assert.equal(validateOptions({ url, format: 'mp4', quality: '1080' }).quality, '1080');
  assert.throws(() => validateOptions({ url, format: 'mp3', quality: '320' }));
  assert.throws(() => validateOptions({ url, format: 'mp4', quality: '2160' }));
  assert.equal(validateOptions({ url, format: 'mp4', quality: '2160' }, 'supporter').quality, '2160');
  assert.throws(() => validateOptions({ url, format: 'm4a', quality: '128' }));
});
