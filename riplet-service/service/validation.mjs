const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const ACCEPTED_HOSTS = new Set(['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be']);
const FORMATS = new Set(['mp3', 'mp4', 'wav']);
const VIDEO_QUALITIES = new Set(['360', '480', '720', '1080', '2160']);
const AUDIO_QUALITIES = new Set(['128', '192', '320']);

export function validateYoutubeUrl(input) {
  if (typeof input !== 'string' || input.length > 1000) throw new Error('Please paste a YouTube video URL.');
  let url;
  try { url = new URL(input.trim()); } catch { throw new Error('Please paste a valid YouTube link.'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.port || !ACCEPTED_HOSTS.has(url.hostname.toLowerCase())) {
    throw new Error('Only HTTPS YouTube video links are supported.');
  }
  const host = url.hostname.toLowerCase();
  const path = url.pathname;
  let id;
  if (host === 'youtu.be' || host === 'www.youtu.be') {
    const parts = path.split('/').filter(Boolean);
    if (parts.length !== 1) throw new Error('Expected a single YouTube video.');
    id = parts[0];
  } else if (path === '/watch' || path === '/watch/') {
    id = url.searchParams.get('v');
  } else {
    const m = /^\/(?:shorts|live|embed)\/([A-Za-z0-9_-]{11})\/?$/.exec(path);
    id = m?.[1];
  }
  if (!id || !VIDEO_ID.test(id)) throw new Error('The link does not contain a valid video ID.');
  return { id, url: `https://www.youtube.com/watch?v=${id}` };
}

export function validateOptions(input, tier = 'free') {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Expected a JSON object.');
  const { id, url } = validateYoutubeUrl(input.url);
  const format = input.format ?? 'mp4';
  if (!FORMATS.has(format)) throw new Error('Supported formats: mp3, mp4, and supporter WAV.');
  const supporter = tier === 'supporter';
  if (format === 'wav' && !supporter) throw new Error('WAV requires an active supporter key.');
  const isAudio = ['mp3', 'm4a', 'wav'].includes(format);
  const quality = String(input.quality ?? (isAudio ? '128' : '720'));
  if (!(isAudio ? AUDIO_QUALITIES : VIDEO_QUALITIES).has(quality)) {
    throw new Error(isAudio ? 'Audio quality must be 128, 192, or 320 kbps.' : 'Video resolution must be 360, 480, 720, 1080, or 2160p.');
  }
  if (!supporter && isAudio && Number(quality) > 192) throw new Error('Higher audio quality requires an active supporter key.');
  if (!supporter && !isAudio && Number(quality) > 1080) throw new Error('Higher video quality requires an active supporter key.');
  return { id, url, format, quality, isAudio };
}

export function safeDownloadName(title, id, ext) {
  const clean = String(title || `video-${id}`)
    .normalize('NFKC')
    .replace(/[\\/\x00-\x1f\x7f<>:"|?*]+/g, ' ')
    .replace(/[.\s]+$/g, '').trim().slice(0, 120) || `video-${id}`;
  return `${clean}.${ext}`;
}
