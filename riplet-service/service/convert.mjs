import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';

const YT_DLP = process.env.YT_DLP_BIN || 'yt-dlp';
const METADATA_TEMPLATE = '{"id":%(id)j,"title":%(title)j,"duration":%(duration)j,"is_live":%(is_live)j,"live_status":%(live_status)j}';

function processArgs() {
  // Node 22+ and yt-dlp[default] (includes yt-dlp-ejs challenge scripts) are installed in the image.
  // We intentionally do not use --remote-components ejs:npm: that mode needs Deno/Bun, not Node.
  return ['--ignore-config', '--no-playlist', '--no-cache-dir', '--use-extractors', 'youtube', '--js-runtimes', 'node',
    '--socket-timeout', '20', '--retries', '2', '--fragment-retries', '2'];
}

export async function runCommand(command, args, { timeoutMs, signal, cwd, onLog }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd, env: { ...process.env, PYTHONUNBUFFERED: '1' }, stdio: ['ignore', 'pipe', 'pipe'],
      detached: process.platform !== 'win32',
    });
    let output = '';
    let exited = false;
    const add = (b) => {
      const line = String(b);
      output = (output + line).slice(-12000);
      if (onLog) onLog(line);
    };
    const kill = () => {
      if (exited) return;
      try {
        if (process.platform !== 'win32' && child.pid) process.kill(-child.pid, 'SIGKILL');
        else child.kill('SIGKILL');
      } catch { child.kill('SIGKILL'); }
    };
    child.stdout.on('data', add);
    child.stderr.on('data', add);
    let didTimeout = false;
    const timer = setTimeout(() => { didTimeout = true; kill(); }, timeoutMs);
    const aborted = () => kill();
    signal?.addEventListener('abort', aborted, { once: true });
    child.once('error', (err) => {
      exited = true; clearTimeout(timer); signal?.removeEventListener('abort', aborted);
      reject(new Error(`Missing or failed executable (${command}): ${err.message}`));
    });
    child.once('close', (code) => {
      if (exited) return;
      exited = true; clearTimeout(timer); signal?.removeEventListener('abort', aborted);
      if (signal?.aborted) return reject(new Error('Conversion cancelled.'));
      if (didTimeout) return reject(new Error('Conversion timed out.'));
      if (code !== 0) return reject(new Error(`Video processing failed. ${output.slice(-1500)}`));
      resolve(output);
    });
  });
}

export async function makeMedia(job, limits) {
  const common = processArgs();
  const meta = await runCommand(YT_DLP,
    [...common, '--skip-download', '--no-warnings', '--print', METADATA_TEMPLATE, job.url],
    { timeoutMs: limits.metadataTimeoutMs, signal: job.controller.signal, onLog: (chunk) => { job.logs = (job.logs + chunk).slice(-4000); } });
  let info;
  try { info = JSON.parse(meta); } catch { throw new Error('Unable to read video metadata.'); }
  if (info.id !== job.videoId) throw new Error('The resolved video ID did not match the URL.');
  if (info.is_live || info.live_status === 'is_live' || info.live_status === 'is_upcoming') {
    throw new Error('Livestreams and upcoming videos are not supported.');
  }
  if (!Number.isFinite(info.duration) || info.duration <= 0) {
    throw new Error('A known video duration is required for this service.');
  }
  if (info.duration > limits.maxDurationSeconds) {
    throw new Error(`Video is longer than the ${limits.maxDurationSeconds / 60}-minute limit.`);
  }
  job.title = typeof info.title === 'string' ? info.title.slice(0, 180) : `video-${job.videoId}`;
  job.phase = 'converting';

  const args = [...common, '--no-warnings', '--no-progress', '--max-filesize', String(limits.maxFileBytes),
    '--output', path.join(job.dir, 'download.%(ext)s')];
  if (job.tier === 'supporter') args.push('--concurrent-fragments', '8');
  if (job.format === 'mp3' || job.format === 'm4a' || job.format === 'wav') {
    args.push('-f', 'bestaudio/best', '-x', '--audio-format', job.format);
    if (job.format !== 'wav') args.push('--audio-quality', `${job.quality}K`);
  } else {
    // Encodes as the requested container; ffmpeg is installed in the Docker image.
    // This may require full transcoding when a compatible source stream is absent.
    const h = Number(job.quality);
    const ext = job.format;
    const preferredAudio = ext === 'mp4' ? 'm4a' : 'webm';
    const compatibleMp4 = ext === 'mp4'
      ? `bv*[height<=${h}][ext=mp4][vcodec^=avc1][protocol=https]+ba[ext=m4a]` : '';
    args.push('-f', `${compatibleMp4 ? `${compatibleMp4}/` : ''}bv*[height<=${h}][ext=${ext}]+ba[ext=${preferredAudio}]/b[height<=${h}][ext=${ext}]/bv*[height<=${h}]+ba/b[height<=${h}]`,
      '--merge-output-format', ext);
  }
  args.push(job.url);

  // yt-dlp's --max-filesize uses available estimates; also enforce a bound on temp disk usage.
  let oversize = false;
  const interval = setInterval(async () => {
    try {
      const files = await fs.readdir(job.dir);
      const stats = await Promise.all(files.map(x => fs.stat(path.join(job.dir, x)).catch(() => null)));
      const total = stats.reduce((n, s) => n + (s?.isFile() ? s.size : 0), 0);
      if (total > limits.maxTempBytes) { oversize = true; job.controller.abort(); }
    } catch { /* Directory can disappear after cancellation. */ }
  }, 1000);
  try {
    await runCommand(YT_DLP, args,
      { timeoutMs: limits.convertTimeoutMs, signal: job.controller.signal,
        onLog: (chunk) => { job.logs = (job.logs + chunk).slice(-4000); } });
  } catch (err) {
    if (oversize) throw new Error('Video exceeds the server file-size limit.');
    throw err;
  } finally { clearInterval(interval); }

  const expected = path.join(job.dir, `download.${job.format}`);
  if (job.format === 'mp4') {
    const probe = await runCommand('ffprobe', ['-v', 'error', '-show_entries', 'stream=codec_name,codec_type', '-of', 'json', expected],
      { timeoutMs: 30000, signal: job.controller.signal });
    let streams;
    try { streams = JSON.parse(probe).streams; } catch { throw new Error('Unable to inspect the MP4 codecs.'); }
    const video = streams?.find(stream => stream.codec_type === 'video');
    const audio = streams?.find(stream => stream.codec_type === 'audio');
    if (!video) throw new Error('Converted MP4 has no video stream.');
    if (video.codec_name !== 'h264' || (audio && audio.codec_name !== 'aac')) {
      const compatible = path.join(job.dir, 'compatible.mp4');
      await runCommand('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-i', expected,
        '-map', '0:v:0', '-map', '0:a:0?', '-c:v', video.codec_name === 'h264' ? 'copy' : 'libx264',
        '-preset', job.tier === 'supporter' ? 'superfast' : 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-c:a', audio?.codec_name === 'aac' ? 'copy' : 'aac',
        '-b:a', '160k', '-movflags', '+faststart', compatible],
      { timeoutMs: limits.convertTimeoutMs, signal: job.controller.signal,
        onLog: chunk => { job.logs = (job.logs + chunk).slice(-4000); } });
      await fs.rename(compatible, expected);
    }
  }
  const stat = await fs.stat(expected).catch(() => null);
  if (!stat || !stat.isFile() || stat.size === 0) throw new Error('Conversion completed without the requested output file.');
  if (stat.size > limits.maxFileBytes) throw new Error('Converted file is larger than the server limit.');
  return { path: expected, bytes: stat.size, title: job.title };
}
