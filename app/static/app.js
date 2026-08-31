const landing = document.querySelector('#landing');
const party = document.querySelector('#party');
const host = document.querySelector('#host');
const kiosk = document.querySelector('#kiosk');
const queue = document.querySelector('#queue');
const results = document.querySelector('#results');
let sessionToken = location.pathname.split('/').filter(Boolean).at(-1);
const isHost = location.pathname.startsWith('/host/');
const isKiosk = location.pathname.startsWith('/kiosk/');
let guestId = localStorage.getItem('music-together-guest');
let player;
let currentItem;
let kioskPlayer;
let wakeLock;
let playbackOwner = 'host';
let playbackRevision = -1;
let kioskPlaybackRevision = -1;
let latestData;
let skipSegments = [];
let skippedSegments = new Set();
let latestKioskQueue = [];
let liveSocket;
const extractToken = value => value.trim().split('/').filter(Boolean).at(-1);
const status = document.querySelector(isHost ? '#host-status' : isKiosk ? '#wake-status' : '#status');

function showStatus(message, isError = false) {
  status.textContent = message;
  status.classList.toggle('error', isError);
}

const authError = new URLSearchParams(location.search).get('auth_error');
if (authError) showStatus(authError, true);

fetch('/api/config').then(response => response.json()).then(config => {
  const kioskPlayback = document.querySelector('#kiosk-playback');
  if (kioskPlayback) kioskPlayback.checked = config.default_playback_owner === 'kiosk';
});

function requestWakeLock() {
  if (!isKiosk) return;
  if (!('wakeLock' in navigator)) {
    document.querySelector('#wake-status').textContent = 'WAKE LOCK UNSUPPORTED';
    return;
  }
  navigator.wakeLock.request('screen').then(lock => {
    wakeLock = lock;
    document.querySelector('#wake-status').textContent = 'SCREEN AWAKE';
    wakeLock.addEventListener('release', () => { document.querySelector('#wake-status').textContent = 'SCREEN WAKE LOCK RELEASED'; });
  }).catch(() => { document.querySelector('#wake-status').textContent = 'WAKE LOCK UNAVAILABLE'; });
}

function controlPlayer(activePlayer, data, revisionKey, force = false, correctDrift = false) {
  if (!activePlayer?.getPlayerState) return revisionKey;
  if (!force && data.playback_revision === revisionKey) {
    if (correctDrift && data.playback_state === 'playing' && data.playback_position > 0 && activePlayer.getCurrentTime && Math.abs(activePlayer.getCurrentTime() - data.playback_position) > 0.75) activePlayer.seekTo(data.playback_position, true);
    return revisionKey;
  }
  if (data.playback_position > 0 && activePlayer.seekTo) activePlayer.seekTo(data.playback_position, true);
  if (activePlayer.setVolume) activePlayer.setVolume(data.playback_volume ?? 80);
  if (data.playback_state === 'playing') activePlayer.playVideo();
  else if (data.playback_state === 'paused') activePlayer.pauseVideo();
  else if (data.playback_state === 'stopped') activePlayer.stopVideo();
  return data.playback_revision;
}

function applyTransport(data, force = false) {
  playbackOwner = data.playback_owner || 'host';
  latestData = data;
  const ownerPlayer = playbackOwner === 'kiosk' ? kioskPlayer : player;
  playbackRevision = controlPlayer(ownerPlayer, data, playbackRevision, force);
  if (isKiosk && kioskPlayer && kioskPlayer !== ownerPlayer) {
    kioskPlayer.mute();
    kioskPlaybackRevision = controlPlayer(kioskPlayer, data, kioskPlaybackRevision, force, true);
  }
}

async function loadSkipSegments(videoId) {
  skipSegments = [];
  skippedSegments = new Set();
  try {
    const response = await fetch(`/api/sessions/${sessionToken}/skip-segments/${encodeURIComponent(videoId)}`);
    if (response.ok) {
      skipSegments = (await response.json()).segments || [];
      skipCurrentSegment();
    }
  } catch (_error) {
    skipSegments = [];
  }
}

function skipCurrentSegment() {
  const activePlayer = playbackOwner === 'kiosk' ? kioskPlayer : player;
  if (!activePlayer?.getCurrentTime || !currentItem) return;
  const currentTime = activePlayer.getCurrentTime();
  const segment = skipSegments.find(item => currentTime >= item.start && currentTime < item.end && !skippedSegments.has(`${item.start}-${item.end}`));
  if (!segment) return;
  skippedSegments.add(`${segment.start}-${segment.end}`);
  activePlayer.seekTo(segment.end, true);
}

setInterval(skipCurrentSegment, 500);

function initHostPlayer() {
  if (!isHost || playbackOwner !== 'host' || player || !window.YT?.Player) return;
    player = new YT.Player('player', {height:'100%', width:'100%', playerVars:{playsinline:1}, events:{onReady: () => { player.unMute(); if (currentItem) player.loadVideoById(currentItem.video_id); if (latestData) applyTransport(latestData, true); }, onStateChange: onPlayerStateChange, onError: () => hostAction('playback-failure')}});
}
if (isHost) {
  window.onYouTubeIframeAPIReady = initHostPlayer;
  const script = document.createElement('script'); script.src = 'https://www.youtube.com/iframe_api'; document.head.appendChild(script);
}
if (isKiosk) {
  window.onYouTubeIframeAPIReady = () => {
    kioskPlayer = new YT.Player('kiosk-video', {height:'100%', width:'100%', playerVars:{playsinline:1, rel:0}, events:{onReady: () => { if (currentItem) kioskPlayer.loadVideoById(currentItem.video_id); if (latestData) applyTransport(latestData, true); if (playbackOwner !== 'kiosk') kioskPlayer.mute(); }, onStateChange: event => { if (event.data === YT.PlayerState.ENDED && playbackOwner === 'kiosk' && currentItem) fetch(`/api/kiosk/${sessionToken}/complete`, {method:'POST'}); }, onError: () => showStatus('Kiosk video unavailable', true)}});
  };
  const script = document.createElement('script'); script.src = 'https://www.youtube.com/iframe_api'; document.head.appendChild(script);
}

function render(data) {
  const previousVideoId = currentItem?.video_id;
  document.querySelector('#playing-kind').textContent = 'ON AIR';
  document.querySelector('#playing').textContent = data.playing?.title || 'Waiting for requests';
  currentItem = data.playing;
  applyTransport(data);
  initHostPlayer();
  if (data.playing?.video_id && data.playing.video_id !== previousVideoId) loadSkipSegments(data.playing.video_id);
  if (isHost && data.playing && data.playing.video_id !== previousVideoId && player?.loadVideoById) player.loadVideoById(data.playing.video_id);
  queue.replaceChildren(...data.queue.map(item => {
    const row = document.createElement('li'); row.className = 'queue-item';
    row.innerHTML = `<span>${item.votes}</span><img class="thumb" src="${item.thumbnail}" alt=""><span><strong>${item.title}</strong><br><small>${item.artist}</small></span><button class="vote ${item.voted ? 'selected' : ''}" data-id="${item.id}">${item.voted ? 'VOTED - REMOVE' : 'VOTE'}</button>`;
    return row;
  }));
  document.querySelectorAll('.vote').forEach(button => button.onclick = () => vote(button.dataset.id));
  if (isHost) renderHost(data);
  if (isKiosk) renderKiosk(data);
}
function renderKiosk(data) {
  landing.classList.add('hidden'); host.classList.add('hidden'); party.classList.add('hidden'); kiosk.classList.remove('hidden');
  document.querySelector('#kiosk-playing-kind').textContent = 'NOW PLAYING';
  document.querySelector('#kiosk-playing').textContent = data.playing?.title || 'Waiting for requests';
  applyTransport(data);
  const kioskVideo = document.querySelector('#kiosk-video');
  if (data.playing?.video_id) {
    const videoId = data.playing.video_id;
    if (kioskVideo.dataset.videoId !== videoId) {
      kioskVideo.dataset.videoId = videoId;
      if (kioskPlayer?.loadVideoById) kioskPlayer.loadVideoById(videoId);
      else kioskVideo.innerHTML = `<iframe src="https://www.youtube.com/embed/${videoId}?autoplay=1&mute=1&playsinline=1&rel=0" title="Current song video" allow="autoplay; encrypted-media" allowfullscreen></iframe>`;
      if (kioskPlayer?.loadVideoById && latestData) setTimeout(() => applyTransport(latestData, true), 0);
    }
  } else { kioskVideo.replaceChildren(); kioskVideo.dataset.videoId = ''; }
  latestKioskQueue = data.queue;
  renderKioskQueue();
  document.querySelector('#qr').src = `/api/qr/${sessionToken}`;
}
function renderKioskQueue() {
  const kioskQueue = document.querySelector('#kiosk-queue');
  if (!kioskQueue) return;
  kioskQueue.replaceChildren(...latestKioskQueue.map(item => {
    const row = document.createElement('li'); row.className = 'kiosk-queue-item';
    row.innerHTML = `<div class="kiosk-thumb"><img src="${item.thumbnail}" alt=""><span>${item.votes}</span></div><strong>${item.title}</strong><small>${item.artist}</small>`;
    return row;
  }));
  if (!kioskQueue.clientHeight) return;
  while (kioskQueue.scrollHeight > kioskQueue.clientHeight && kioskQueue.lastElementChild) kioskQueue.lastElementChild.remove();
}
async function addResult(item) {
  const response = await fetch(`/api/sessions/${sessionToken}/songs`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:`https://youtu.be/${item.video_id}`, title:item.title, artist:item.artist})});
  const data = await response.json();
  if (!response.ok) return showStatus(data.detail || 'Could not add song', true);
  guestId = data.guest_id; localStorage.setItem('music-together-guest', guestId); render(data); showStatus('Added to the queue.');
}
function renderHost(data) {
  landing.classList.add('hidden'); kiosk.classList.add('hidden'); host.classList.remove('hidden'); party.classList.add('hidden');
  document.querySelector('#player').classList.toggle('hidden', data.playback_owner !== 'host');
  if (data.guest_url) document.querySelector('#host-guest-url').value = data.guest_url;
  if (data.kiosk_url) document.querySelector('#host-kiosk-url').value = data.kiosk_url;
  const hostQueue = document.querySelector('#host-queue');
  hostQueue.replaceChildren(...data.queue.map(item => {
    const row = document.createElement('li'); row.className = 'queue-item';
    row.innerHTML = `<span>${item.votes}</span><img class="thumb" src="${item.thumbnail}" alt=""><span><strong>${item.title}</strong></span><span><button data-action="play" data-id="${item.id}">PLAY</button> <button data-action="remove" data-id="${item.id}">REMOVE</button></span>`;
    return row;
  }));
  hostQueue.querySelectorAll('button').forEach(button => button.onclick = () => hostAction(button.dataset.action, button.dataset.id));
}
async function load() {
  if (!sessionToken || sessionToken === 'guest' || sessionToken === 'kiosk') return;
  document.body.classList.toggle('kiosk-mode', isKiosk);
  if (isKiosk) {
    document.querySelector('#add-form').classList.add('hidden');
    document.querySelector('#search-form').classList.add('hidden');
  }
  const response = await fetch(isHost ? `/api/host/${sessionToken}` : `/api/sessions/${sessionToken}`);
  if (!response.ok) return;
  landing.classList.add('hidden'); party.classList.remove('hidden'); render(await response.json());
  liveSocket?.close();
  liveSocket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${sessionToken}`);
  liveSocket.onmessage = event => render(JSON.parse(event.data));
}
async function vote(id) {
  const response = await fetch(`/api/sessions/${sessionToken}/queue/${id}/vote`, {method:'POST'});
  const data = await response.json();
  if (!response.ok) return showStatus(data.detail || 'Could not vote', true);
  guestId = data.guest_id; localStorage.setItem('music-together-guest', guestId);
  liveSocket?.close();
  render(data);
  load();
}
async function hostAction(action, id) { const path = id ? `/queue/${id}/${action}` : `/${action}`; const response = await fetch(`/api/host/${sessionToken}${path}`, {method:'POST'}); if (response.ok) render(await response.json()); }
async function setTransport(state) { const activePlayer = playbackOwner === 'kiosk' ? null : player; const position = activePlayer?.getCurrentTime ? activePlayer.getCurrentTime() : 0; const volume = Number(document.querySelector('#volume').value); const response = await fetch(`/api/host/${sessionToken}/transport`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({state, position, volume})}); if (response.ok) render(await response.json()); }
setInterval(() => {
  if (!isHost || playbackOwner !== 'host' || !player?.getPlayerState || player.getPlayerState() !== YT.PlayerState.PLAYING) return;
  fetch(`/api/host/${sessionToken}/transport-position`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({position:player.getCurrentTime()})});
}, 1000);
async function onPlayerStateChange(event) {
  if (event.data !== YT.PlayerState.ENDED) return;
  if (currentItem) {
    await hostAction('complete', currentItem.id);
    return;
  }
  const response = await fetch(`/api/host/${sessionToken}/next`, {method:'POST'});
  render(await response.json());
}
let createdHostUrl;
document.querySelector('#create').onclick = async () => {
  const response = await fetch('/api/sessions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({kiosk_playback:document.querySelector('#kiosk-playback').checked})});
  const data = await response.json();
  if (response.status === 401) return location.href = '/auth/login';
  if (!response.ok) return showStatus(data.detail || 'Could not create session', true);
  createdHostUrl = data.host_url;
  document.querySelector('#host-url').value = data.host_url;
  document.querySelector('#guest-url').value = data.guest_url;
  document.querySelector('#kiosk-url').value = data.kiosk_url;
  document.querySelector('#session-links').classList.remove('hidden');
  showStatus('Session created.');
};
document.querySelector('#search-form').onsubmit = async event => { event.preventDefault(); const response = await fetch('/api/search', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({query:document.querySelector('#search-query').value})}); const data = await response.json(); if (!response.ok) return showStatus(data.detail || 'Search unavailable', true); results.replaceChildren(...(data.results || []).map(item => { const row = document.createElement('li'); row.className = 'queue-item search-result'; row.innerHTML = `<img class="thumb" src="${item.thumbnail || `https://i.ytimg.com/vi/${item.video_id}/hqdefault.jpg`}" alt=""><span><strong>${item.title}</strong><br><small>${item.artist}</small></span><button type="button">ADD</button>`; row.querySelector('button').onclick = () => addResult(item); return row; })); };
document.querySelector('#clear-search').onclick = () => { document.querySelector('#search-query').value = ''; results.replaceChildren(); document.querySelector('#search-query').focus(); };
document.querySelector('#add-form').onsubmit = async event => { event.preventDefault(); const response = await fetch(`/api/sessions/${sessionToken}/songs`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:document.querySelector('#url').value, title:document.querySelector('#title').value})}); const data = await response.json(); guestId = data.guest_id; localStorage.setItem('music-together-guest', guestId); render(data); event.target.reset(); };
document.querySelector('#playlist-form').onsubmit = async event => {
  event.preventDefault();
  const response = await fetch(`/api/host/${sessionToken}/playlist`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:document.querySelector('#playlist-url').value})});
  const data = await response.json();
  if (!response.ok) return showStatus(data.detail || 'Could not add playlist', true);
  render(data); event.target.reset(); showStatus('Playlist added to the queue.');
};
document.querySelector('#pause')?.addEventListener('click', () => setTransport('paused'));
document.querySelector('#stop')?.addEventListener('click', () => setTransport('stopped'));
document.querySelector('#resume')?.addEventListener('click', () => setTransport('playing'));
document.querySelector('#volume')?.addEventListener('input', () => setTransport('playing'));
function activateKioskPlayback() {
  if (playbackOwner !== 'kiosk') return;
  if (kioskPlayer?.setVolume) kioskPlayer.setVolume(latestData?.playback_volume ?? 80);
  kioskPlayer?.unMute?.();
  if (latestData?.playback_state === 'playing') kioskPlayer?.playVideo?.();
  requestWakeLock();
}
document.addEventListener('click', () => {
  if (isKiosk && playbackOwner === 'kiosk') activateKioskPlayback();
}, {once:false});
document.querySelector('#skip-current')?.addEventListener('click', () => { if (currentItem) hostAction('skip', currentItem.id); });
document.querySelector('#end-session')?.addEventListener('click', async () => { if (!confirm('End this party session?')) return; await fetch(`/api/host/${sessionToken}/end`, {method:'POST'}); location.href = '/'; });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') requestWakeLock(); });
if (window.ResizeObserver) new ResizeObserver(renderKioskQueue).observe(document.querySelector('#kiosk-queue'));
document.querySelectorAll('[data-copy]').forEach(button => button.onclick = async () => {
  const input = document.querySelector(`#${button.dataset.copy}`);
  if (!input.value) return showStatus('The session link is not ready yet.', true);
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(input.value);
    } else {
      const helper = document.createElement('textarea');
      helper.value = input.value;
      helper.style.position = 'fixed';
      helper.style.opacity = '0';
      document.body.appendChild(helper);
      helper.focus();
      helper.select();
      if (!document.execCommand('copy')) throw new Error('Copy command failed');
      helper.remove();
    }
  } catch (_error) {
    input.focus();
    input.select();
    showStatus('Copy was blocked. The URL is selected for manual copying.', true);
    return;
  }
  const original = button.textContent;
  button.textContent = 'COPIED';
  setTimeout(() => button.textContent = original, 1200);
});
document.querySelector('#open-host').onclick = () => { if (createdHostUrl) location.href = createdHostUrl; };
load();
