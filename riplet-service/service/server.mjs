import http from 'node:http';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { promises as fs, createReadStream } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { validateOptions, safeDownloadName } from './validation.mjs';
import { makeMedia } from './convert.mjs';
import { createSupport } from './support.mjs';
import { createMetrics } from './metrics.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.resolve(HERE, '..', 'public');
const num = (env, fallback) => { const n = Number(process.env[env]); return Number.isFinite(n) && n > 0 ? Math.floor(n) : fallback; };
const config = {
  port: num('PORT', 8080),
  host: process.env.HOST || '0.0.0.0',
  allowedOrigins: new Set((process.env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean)),
  trustProxy: process.env.TRUST_PROXY === 'true',
  maxActive: num('MAX_ACTIVE_JOBS', 2),
  maxIpActive: num('MAX_ACTIVE_PER_IP', 1),
  maxRequestsPerHour: num('REQUESTS_PER_IP_PER_HOUR', 12),
  maxReadyJobs: num('MAX_READY_JOBS', 20),
  readyLifetimeMs: num('READY_TTL_SECONDS', 300) * 1000,
  maxDurationSeconds: num('MAX_DURATION_SECONDS', 1800),
  maxFileBytes: num('MAX_FILE_BYTES', 268435456),
  maxTempBytes: num('MAX_TEMP_BYTES', 400000000),
  metadataTimeoutMs: num('METADATA_TIMEOUT_SECONDS', 60) * 1000,
  convertTimeoutMs: num('CONVERT_TIMEOUT_SECONDS', 600) * 1000,
};
const support = createSupport({
  dataDir: process.env.SUPPORTER_DATA_DIR,
  verificationToken: process.env.KOFI_VERIFICATION_TOKEN,
  resendApiKey: process.env.RESEND_API_KEY,
  fromEmail: process.env.SUPPORT_FROM_EMAIL,
});
const metrics = createMetrics(process.env.METRICS_DB_PATH || path.join(HERE, '..', 'data', 'metrics.sqlite'));
const adminToken = process.env.RIPLET_ADMIN_TOKEN || '';
const jobs = new Map();
const hits = new Map();
const supportHits = new Map();
function supportLimit(key, count, windowMs) {
  const now = Date.now();
  const recent = (supportHits.get(key) || []).filter(time => now - time < windowMs);
  if (recent.length >= count) return true;
  recent.push(now); supportHits.set(key, recent);
  return false;
}
let activeCount = 0;
const activeByIp = new Map();
const json = (res, code, value, headers = {}) => {
  if (res.headersSent) return;
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store', ...headers });
  res.end(JSON.stringify(value));
};
const reason = (err) => (err?.message || 'Conversion failed.').slice(0, 600);
const removeDir = async (job) => { if (job.dir) await fs.rm(job.dir, { recursive: true, force: true }).catch(() => {}); };

async function deleteJob(job) {
  if (!jobs.has(job.id)) return;
  jobs.delete(job.id);
  job.controller.abort();
  await removeDir(job);
}

async function runJob(job) {
  activeCount++;
  activeByIp.set(job.ip, (activeByIp.get(job.ip) || 0) + 1);
  try {
    job.status = 'working';
    job.phase = 'reading metadata';
    job.dir = await fs.mkdtemp(path.join(process.env.JOB_TEMP_DIR || os.tmpdir(), 'youtube-converter-'));
    const media = await makeMedia(job, {
      ...config,
      maxDurationSeconds: job.tier === 'supporter' ? (job.isAudio ? 43_200 : 14_400) : (job.isAudio ? 14_400 : 5_400),
      maxFileBytes: job.tier === 'supporter' ? 16_000_000_000 : (job.isAudio ? 300_000_000 : 2_000_000_000),
      maxTempBytes: job.tier === 'supporter' ? 24_000_000_000 : 3_000_000_000,
    });
    if (job.controller.signal.aborted) return;
    job.mediaPath = media.path;
    job.bytes = media.bytes;
    job.title = media.title;
    job.status = 'ready';
    job.phase = 'ready';
    job.expiresAt = Date.now() + config.readyLifetimeMs;
    metrics.record('job_completed', { ...job, durationMs: Date.now() - job.createdAt });
  } catch (err) {
    if (job.controller.signal.aborted && !jobs.has(job.id)) return;
    job.status = 'failed';
    job.phase = 'failed';
    // Do not expose full yt-dlp logs (which might have private server context) in public responses.
    const msg = reason(err);
    job.error = /limit|timed out|cancelled|duration|not supported/i.test(msg) ? msg :
      'This video could not be processed. It may be unavailable, restricted, or temporarily blocked by YouTube.';
    job.internalError = msg;
    metrics.record('job_failed', { ...job, durationMs: Date.now() - job.createdAt, reason: /limit|timeout|unavailable|blocked|format|network/i.exec(msg.toLowerCase())?.[0] || 'conversion_error' });
    console.error(`[${job.id}] ${msg}`);
    job.expiresAt = Date.now() + 60_000;
    await removeDir(job);
  } finally {
    activeCount--;
    activeByIp.set(job.ip, Math.max(0, (activeByIp.get(job.ip) || 1) - 1));
  }
}

function canCreate(ip) {
  const now = Date.now();
  const times = (hits.get(ip) || []).filter(t => now - t < 3_600_000);
  hits.set(ip, times);
  if (times.length >= config.maxRequestsPerHour) return 'Hourly conversion limit reached. Try again later.';
  if (activeCount >= config.maxActive || (activeByIp.get(ip) || 0) >= config.maxIpActive) {
    return 'The converter is busy. Please try again shortly.';
  }
  if ([...jobs.values()].filter(j => j.status === 'ready').length >= config.maxReadyJobs) return 'The converter is at capacity. Please try again shortly.';
  return null;
}

async function readBody(req) {
  if (!String(req.headers['content-type'] || '').startsWith('application/json')) throw new Error('Send application/json.');
  let size = 0, chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 16_384) throw new Error('Request is too large.');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new Error('Invalid JSON.'); }
}

function clientIp(req) {
  if (config.trustProxy) {
    const forwarded = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
    if (forwarded && forwarded.length < 100) return forwarded;
  }
  return req.socket.remoteAddress || 'unknown';
}

function setOriginHeaders(req, res) {
  const origin = req.headers.origin;
  if (!origin) return true; // curl / same-origin requests without Origin still subject to IP limits.
  let sameOrigin = false;
  try { sameOrigin = new URL(origin).host === req.headers.host; } catch {}
  if (!sameOrigin && !config.allowedOrigins.has(origin)) return false;
  res.setHeader('Access-Control-Allow-Origin', origin);
  res.setHeader('Vary', 'Origin');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Riplet-Key');
  res.setHeader('Access-Control-Expose-Headers', 'Content-Disposition, Content-Length');
  return true;
}

async function serve(req, res) {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/_admin')) {
    if (!adminToken) return json(res, 404, { error: 'Not found.' });
    const authorization = String(req.headers.authorization || '');
    let supplied = authorization.replace(/^Bearer /i, '');
    if (/^Basic /i.test(authorization)) {
      const decoded = Buffer.from(authorization.slice(6), 'base64').toString('utf8');
      supplied = decoded.startsWith('admin:') ? decoded.slice(6) : '';
    }
    const a = Buffer.from(supplied), b = Buffer.from(adminToken);
    if (a.length !== b.length || !crypto.timingSafeEqual(a,b)) return json(res, 401, { error: 'Unauthorized.' }, { 'WWW-Authenticate': 'Basic realm="Riplet monitoring"' });
    if (url.pathname === '/_admin/metrics' && req.method === 'GET') {
      const hours = [24,168,720].includes(Number(url.searchParams.get('hours'))) ? Number(url.searchParams.get('hours')) : 24;
      const disk = await fs.statfs(process.env.JOB_TEMP_DIR || os.tmpdir()).catch(() => null);
      return json(res, 200, { ...metrics.snapshot(hours), live: {
        activeJobs: activeCount, queueLength: 0, concurrencyLimit: config.maxActive,
        cpuLoad: os.loadavg(), freeMemory: os.freemem(), totalMemory: os.totalmem(),
        diskFree: disk ? disk.bavail * disk.bsize : null,
        diskTotal: disk ? disk.blocks * disk.bsize : null,
        vpnInterface: await fs.access('/sys/class/net/tun0').then(()=>true).catch(()=>false),
      }});
    }
    if (url.pathname === '/_admin' && req.method === 'GET') {
      const page = await fs.readFile(path.join(PUBLIC,'admin.html'));
      res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});
      return res.end(page);
    }
    return json(res, 404, { error: 'Not found.' });
  }
  // Browser access restrictions are not authentication. Protect public deployments with a WAF/rate limits.
  if (!setOriginHeaders(req, res)) { metrics.record('blocked_request',{reason:'origin'}); return json(res, 403, { error: 'Origin not allowed.' }); }
  if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }
  if (url.pathname === '/api/health' && req.method === 'GET') return json(res, 200, { ok: true, name: 'youtube-converter-api' });
  if (url.pathname === '/api/analytics/pageview' && req.method === 'POST') {
    let body;
    try { body = await readBody(req); } catch { return json(res, 400, { error: 'Invalid pageview.' }); }
    let referrer = 'Direct';
    try { if (body.referrer) referrer = new URL(body.referrer).hostname.slice(0, 100); } catch {}
    const country = String(req.headers['cf-ipcountry'] || 'Unknown').replace(/[^A-Z]/g, '').slice(0, 2) || 'Unknown';
    const agent = String(req.headers['user-agent'] || '');
    const device = /mobile|iphone|android/i.test(agent) ? 'Mobile' : /ipad|tablet/i.test(agent) ? 'Tablet' : 'Desktop';
    metrics.record('pageview', { referrer, country, device });
    if (body.visit === true) metrics.record('visit', { country, device });
    return json(res, 204, null);
  }
  if (url.pathname === '/api/support/status' && req.method === 'GET') {
    return json(res, 200, await support.status(req.headers['x-riplet-key']));
  }
  if (url.pathname === '/api/support/activate' && req.method === 'POST') {
    if (supportLimit(`activate:${clientIp(req)}`, 20, 3_600_000)) return json(res, 429, { error: 'Too many activation attempts.' });
    let body;
    try { body = await readBody(req); } catch (err) { return json(res, 400, { error: reason(err) }); }
    const result = await support.status(body.key);
    return json(res, result.active ? 200 : 401, result.active ? result : { error: 'Invalid or expired key.' });
  }
  if (url.pathname === '/api/support/resend' && req.method === 'POST') {
    if (supportLimit(`resend-ip:${clientIp(req)}`, 4, 3_600_000)) return json(res, 429, { error: 'Please wait before requesting another key.' });
    let body;
    try { body = await readBody(req); } catch (err) { return json(res, 400, { error: reason(err) }); }
    if (typeof body.email === 'string' && supportLimit(`resend-email:${body.email.trim().toLowerCase()}`, 1, 900_000)) return json(res, 429, { error: 'Please wait before requesting another key.' });
    const result = await support.resend(body.email);
    return json(res, result.status, result.body);
  }
  if (url.pathname === '/api/support/kofi-webhook' && req.method === 'POST') {
    if (!String(req.headers['content-type'] || '').startsWith('application/x-www-form-urlencoded')) return json(res, 415, { error: 'Expected form data.' });
    let raw = '';
    for await (const chunk of req) { raw += chunk; if (raw.length > 32_768) return json(res, 413, { error: 'Payload too large.' }); }
    let payload;
    try { payload = JSON.parse(new URLSearchParams(raw).get('data') || ''); }
    catch { return json(res, 400, { error: 'Invalid payment data.' }); }
    const result = await support.processWebhook(payload);
    return json(res, result.status, result.body);
  }


  if (url.pathname === '/api/jobs' && req.method === 'POST') {
    const ip = clientIp(req);
    const denied = canCreate(ip);
    if (denied) { metrics.record('rate_limited',{reason:denied.includes('Hourly')?'hourly':'capacity'}); return json(res, 429, { error: denied }, { 'Retry-After': '15' }); }
    let options;
    const entitlement = await support.status(req.headers['x-riplet-key']);
    const tier = entitlement.active ? 'supporter' : 'free';
    try { options = validateOptions(await readBody(req), tier); }
    catch (err) { metrics.record('invalid_request',{reason:/url|link|video/i.test(reason(err))?'url':'options'}); return json(res, 400, { error: reason(err) }); }
    const id = crypto.randomUUID();
    const job = { ...options, tier, videoId: options.id, id, ip, status: 'working', phase: 'starting', createdAt: Date.now(),
      controller: new AbortController(), title: '', bytes: 0, logs: '', dir: '', expiresAt: null };
    jobs.set(id, job);
    metrics.record('job_started', job);
    hits.get(ip).push(Date.now());
    void runJob(job);
    return json(res, 202, {
      id, status: job.status, statusUrl: `/api/jobs/${id}`, downloadUrl: `/api/jobs/${id}/download`,
    });
  }
  const match = /^\/api\/jobs\/([0-9a-f-]{36})(\/download)?$/.exec(url.pathname);
  if (match) {
    const id = match[1];
    const job = jobs.get(id);
    if (!job) return json(res, 404, { error: 'This download is unavailable or has expired.' });
    if (req.method === 'DELETE' && !match[2]) { metrics.record('job_cancelled',{...job,durationMs:Date.now()-job.createdAt}); await deleteJob(job); return json(res, 200, { cancelled: true }); }
    if (req.method !== 'GET') return json(res, 405, { error: 'Method not allowed.' });
    if (!match[2]) {
      return json(res, 200, {
        id: job.id, status: job.status, phase: job.phase,
        format: job.format, quality: job.quality, title: job.title || null,
        ...(job.status === 'ready' ? { bytes: job.bytes, downloadUrl: `/api/jobs/${job.id}/download`, expiresAt: new Date(job.expiresAt).toISOString() } : {}),
        ...(job.status === 'failed' ? { error: job.error } : {}),
      });
    }
    if (job.status !== 'ready') return json(res, 409, { error: 'The file is not ready.' });
    if (job.downloading) return json(res, 409, { error: 'Download already started.' });
    job.downloading = true;
    const filename = safeDownloadName(job.title, job.videoId, job.format);
    const asciiName = `youtube-${job.videoId}.${job.format}`;
    const contentType = ({ mp3: 'audio/mpeg', m4a: 'audio/mp4', wav: 'audio/wav', mp4: 'video/mp4', webm: 'video/webm' })[job.format];
    res.writeHead(200, {
      'Content-Type': contentType,
      'Content-Length': job.bytes,
      'Content-Disposition': `attachment; filename="${asciiName}"; filename*=UTF-8''${encodeURIComponent(filename)}`,
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    });
    const stream = createReadStream(job.mediaPath);
    let cleaned = false;
    let delivered=0, finished=false;
    stream.on('data',chunk=>{ delivered+=chunk.length; });
    res.once('finish',()=>{ finished=true; metrics.record('download_completed',{...job,delivered}); });
    const cleanup = () => { if (!cleaned) { cleaned = true; if (!finished) metrics.record('download_interrupted',{...job,delivered}); void deleteJob(job); } };
    stream.once('error', () => { res.destroy(); cleanup(); });
    res.once('close', cleanup); // triggers after completion or aborted transfer
    stream.pipe(res);
    return;
  }

  if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/app.js')) {
    const file = path.join(PUBLIC, url.pathname === '/' ? 'index.html' : 'app.js');
    try {
      const data = await fs.readFile(file);
      res.writeHead(200, { 'Content-Type': url.pathname === '/' ? 'text/html; charset=utf-8' : 'application/javascript; charset=utf-8',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      return res.end(data);
    } catch { /* File not packaged. */ }
  }
  json(res, 404, { error: 'Not found.' });
}

const server = http.createServer((req, res) => {
  void serve(req, res).catch(err => {
    console.error('Request error:', reason(err));
    metrics.record('api_error',{reason:'internal'});
    if (!res.headersSent) json(res, 500, { error: 'Internal server error.' });
    else res.destroy();
  });
});

const sweeper = setInterval(() => {
  const now = Date.now();
  for (const job of jobs.values()) {
    if (job.expiresAt && now > job.expiresAt && !job.downloading) void deleteJob(job);
  }
  for (const [ip, times] of hits.entries()) {
    const recent = times.filter(t => now - t < 3_600_000);
    if (recent.length) hits.set(ip, recent); else hits.delete(ip);
  }
  for (const [key, times] of supportHits.entries()) {
    const recent = times.filter(time => now - time < 3_600_000);
    if (recent.length) supportHits.set(key, recent); else supportHits.delete(key);
  }
}, 15_000);
sweeper.unref();
const metricsPruner = setInterval(() => metrics.prune(), 3_600_000);
metricsPruner.unref();

if (process.env.NODE_ENV !== 'test-import') {
  server.listen(config.port, config.host, () => {
    console.log(`Converter API ready at http://${config.host}:${config.port}`);
  });
}

export { server, config, jobs, metrics };
