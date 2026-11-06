import crypto from 'node:crypto';
import path from 'node:path';
import { promises as fs } from 'node:fs';

const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
const days = 15 * 24 * 60 * 60 * 1000;
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const validEmail = value => typeof value === 'string' && value.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
const normalize = key => String(key || '').toUpperCase().trim().replace(/\s/g, '');
const safeCompare = (a, b) => {
  const aa = Buffer.from(String(a || '')), bb = Buffer.from(String(b || ''));
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
};
function newKey() {
  const bytes = crypto.randomBytes(20);
  const chars = [...bytes].map(byte => alphabet[byte % alphabet.length]).join('');
  return `RP-${chars.match(/.{1,5}/g).join('-')}`;
}
function seal(key, secret) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', crypto.createHash('sha256').update(secret).digest(), iv);
  const ciphertext = Buffer.concat([cipher.update(key, 'utf8'), cipher.final()]);
  return [iv, cipher.getAuthTag(), ciphertext].map(x => x.toString('base64url')).join('.');
}
function unseal(value, secret) {
  const [iv, tag, ciphertext] = String(value).split('.').map(x => Buffer.from(x, 'base64url'));
  const decipher = crypto.createDecipheriv('aes-256-gcm', crypto.createHash('sha256').update(secret).digest(), iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
}

export function createSupport({ dataDir, verificationToken, resendApiKey, fromEmail, fetcher = fetch, clock = Date.now }) {
  const enabled = Boolean(dataDir && verificationToken && resendApiKey && fromEmail);
  const file = path.join(dataDir || '/tmp', 'supporters.json');
  let chain = Promise.resolve();
  const locked = async fn => {
    const previous = chain;
    let release;
    chain = new Promise(resolve => { release = resolve; });
    await previous;
    try { return await fn(); } finally { release(); }
  };
  async function read() {
    try { return JSON.parse(await fs.readFile(file, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return { payments: {} }; throw error; }
  }
  async function write(state) {
    await fs.mkdir(dataDir, { recursive: true, mode: 0o700 });
    const temp = `${file}.${crypto.randomUUID()}.tmp`;
    await fs.writeFile(temp, JSON.stringify(state), { mode: 0o600 });
    await fs.rename(temp, file);
  }
  async function sendKey(email, key, paymentId) {
    const response = await fetcher('https://api.resend.com/emails', {
      method: 'POST',
      headers: { Authorization: `Bearer ${resendApiKey}`, 'Content-Type': 'application/json', 'Idempotency-Key': `riplet-key-${hash(paymentId).slice(0,40)}` },
      body: JSON.stringify({ from: fromEmail, to: [email], subject: 'Your Riplet supporter key',
        text: `Thank you for supporting Riplet. Your activation key is ${key}. Open riplet.cc, tap Support us, then Activate your key. The key is valid for 15 days from your payment.` }),
      signal: AbortSignal.timeout(20_000),
    });
    if (!response.ok) throw new Error(`Email delivery failed (${response.status}).`);
  }
  async function processWebhook(payload) {
    if (!enabled) return { status: 503, body: { error: 'Supporter payments are not configured.' } };
    if (!safeCompare(payload?.verification_token, verificationToken)) return { status: 403, body: { error: 'Invalid webhook token.' } };
    const paymentId = String(payload.kofi_transaction_id || payload.message_id || '');
    if (!/^[0-9a-f-]{36}$/i.test(paymentId)) return { status: 400, body: { error: 'Invalid payment ID.' } };
    const amount = Number(payload.amount);
    if (!['Tip', 'Donation', 'Subscription'].includes(payload.type) || payload.currency !== 'USD' || !Number.isFinite(amount) || amount < 5) {
      return { status: 200, body: { ok: true, ignored: true } };
    }
    if (!validEmail(payload.email)) return { status: 400, body: { error: 'Payment email is missing.' } };
    const email = payload.email.toLowerCase();
    return locked(async () => {
      const state = await read();
      let record = state.payments[paymentId];
      if (record?.status === 'issued') return { status: 200, body: { ok: true, duplicate: true } };
      if (!record) {
        const key = newKey();
        record = { status: 'pending', email, keyHash: hash(key), sealedKey: seal(key, verificationToken),
          expiresAt: clock() + days, createdAt: clock() };
        state.payments[paymentId] = record;
        await write(state);
      }
      if (record.email !== email) return { status: 409, body: { error: 'Payment ID collision.' } };
      const key = unseal(record.sealedKey, verificationToken);
      await sendKey(email, key, paymentId);
      record.status = 'issued';
      delete record.sealedKey;
      await write(state);
      return { status: 200, body: { ok: true } };
    });
  }
  async function status(key) {
    if (!enabled) return { active: false, configured: false };
    const candidate = normalize(key);
    if (!/^RP-(?:[23456789A-HJ-NP-Z]{5}-){3}[23456789A-HJ-NP-Z]{5}$/.test(candidate)) return { active: false, configured: true };
    const state = await read();
    const keyHash = hash(candidate);
    const record = Object.values(state.payments).find(item => item.status === 'issued' && item.keyHash === keyHash && item.expiresAt > clock());
    return record ? { active: true, configured: true, expiresAt: new Date(record.expiresAt).toISOString() } : { active: false, configured: true };
  }
  async function resend(email) {
    if (!enabled) return { status: 503, body: { error: 'Supporter email is not configured.' } };
    if (!validEmail(email)) return { status: 400, body: { error: 'Enter a valid email address.' } };
    return locked(async () => {
      const state = await read();
      const matching = Object.entries(state.payments).filter(([, record]) => record.status === 'issued' && record.email === email.toLowerCase() && record.expiresAt > clock());
      const latest = matching.sort((a,b) => b[1].expiresAt - a[1].expiresAt)[0];
      if (!latest) return { status: 200, body: { ok: true } };
      const [paymentId, record] = latest;
      const key = newKey();
      await sendKey(email.toLowerCase(), key, `${paymentId}-resend-${crypto.randomUUID()}`);
      record.keyHash = hash(key);
      await write(state);
      return { status: 200, body: { ok: true } };
    });
  }
  return { enabled, processWebhook, status, resend };
}
