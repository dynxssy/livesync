'use strict';
/**
 * LiveSync relay server.
 *
 * Both Live users connect *out* to this server over WebSocket, so nobody has to
 * open ports or be on the same network. The server:
 *   - groups peers into rooms identified by a short code,
 *   - gives every change ("op") a global sequence number so both sets apply
 *     edits in the same order,
 *   - keeps a short op log so a peer that drops off for a moment catches up
 *     without a full resync,
 *   - brokers full-set snapshots from the host to joining/resyncing peers,
 *   - relays audio files (recorded/imported samples) and caches them on disk.
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { WebSocketServer } = require('ws');

const PROTOCOL_VERSION = 1;
const OP_LOG_LIMIT = parseInt(process.env.OP_LOG_LIMIT || '20000', 10);
const MAX_ROOM_PEERS = parseInt(process.env.MAX_ROOM_PEERS || '8', 10);
const ROOM_IDLE_MS = parseInt(process.env.ROOM_IDLE_MINUTES || '720', 10) * 60 * 1000;
const MAX_FILE_MB = parseInt(process.env.MAX_FILE_MB || '1024', 10);
const MAX_MESSAGE_MB = 8;

function createServer(opts = {}) {
  const dataDir = opts.dataDir || process.env.DATA_DIR || path.join(__dirname, '..', 'data');
  const filesDir = path.join(dataDir, 'files');
  fs.mkdirSync(filesDir, { recursive: true });
  const log = opts.quiet ? () => {} : (...a) => console.log(new Date().toISOString(), ...a);

  /** @type {Map<string, Room>} */
  const rooms = new Map();
  /** uploads in progress: hash -> {fd, received:Set, total, size, name, tmp} */
  const uploads = new Map();

  const httpServer = http.createServer((req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true, rooms: rooms.size, version: PROTOCOL_VERSION }));
      return;
    }
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end('LiveSync relay is running.\n');
  });

  const wss = new WebSocketServer({ server: httpServer, maxPayload: MAX_MESSAGE_MB * 1024 * 1024 });

  function send(ws, msg) {
    if (ws.readyState === 1) ws.send(JSON.stringify(msg));
  }

  function newCode() {
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    let code;
    do {
      code = '';
      for (let i = 0; i < 6; i++) code += alphabet[crypto.randomInt(alphabet.length)];
    } while (rooms.has(code));
    return code;
  }

  function normCode(code) {
    return String(code || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  }

  function peerList(room) {
    return [...room.peers.values()].map((p) => ({
      id: p.id, name: p.name, host: p.id === room.hostId, latency: p.latency, hash: p.lastHash || null,
    }));
  }

  function broadcastPeers(room) {
    const peers = peerList(room);
    for (const p of room.peers.values()) send(p.ws, { t: 'peers', peers, host: room.hostId, you: p.id });
  }

  function pickHost(room) {
    // The longest-connected peer becomes host: its set is the most likely to be complete.
    let best = null;
    for (const p of room.peers.values()) if (!best || p.joinedAt < best.joinedAt) best = p;
    room.hostId = best ? best.id : null;
  }

  function requestSnapshot(room, forPeer) {
    const host = room.peers.get(room.hostId);
    if (!host) return;
    if (host.id === forPeer.id) {
      send(forPeer.ws, { t: 'snapshot_none', reason: 'you_are_host' });
      return;
    }
    room.pendingSnapshots.add(forPeer.id);
    send(host.ws, { t: 'snapshot_request', for: forPeer.id });
  }

  function handleHello(ws, peer, msg) {
    if (msg.v !== PROTOCOL_VERSION) {
      send(ws, { t: 'error', code: 'version', message: 'Your LiveSync version does not match the server. Please update LiveSync.' });
      ws.close();
      return;
    }
    let code = normCode(msg.room);
    let room;
    if (msg.create || !code) {
      code = code && !rooms.has(code) ? code : newCode();
      room = {
        code, peers: new Map(), hostId: null, seq: 0, log: [], logStart: 1,
        password: msg.password ? String(msg.password) : '', pendingSnapshots: new Set(),
        lastActive: Date.now(), files: new Set(),
      };
      rooms.set(code, room);
      log('room created', code);
    } else {
      room = rooms.get(code);
      if (!room) {
        send(ws, { t: 'error', code: 'no_room', message: `No session with code ${code}. Check the code with your partner.` });
        return;
      }
      if (room.password && room.password !== String(msg.password || '')) {
        send(ws, { t: 'error', code: 'password', message: 'Wrong session password.' });
        return;
      }
      if (room.peers.size >= MAX_ROOM_PEERS) {
        send(ws, { t: 'error', code: 'full', message: 'That session is full.' });
        return;
      }
    }
    // A reconnecting peer reuses its old identity so others don't see a "new" person.
    const resumeId = msg.peer && !room.peers.has(msg.peer) ? String(msg.peer) : null;
    peer.id = resumeId || crypto.randomBytes(4).toString('hex');
    peer.name = String(msg.name || 'Producer').slice(0, 40);
    peer.room = room;
    peer.joinedAt = msg.joinedAt && resumeId ? msg.joinedAt : Date.now();
    room.peers.set(peer.id, peer);
    if (!room.hostId || !room.peers.has(room.hostId)) pickHost(room);
    room.lastActive = Date.now();

    const isHost = room.hostId === peer.id;
    send(ws, {
      t: 'welcome', room: code, peer: peer.id, host: isHost, seq: room.seq,
      joinedAt: peer.joinedAt, peers: peerList(room),
    });
    log('peer', peer.id, peer.name, 'joined', code, isHost ? '(host)' : '');

    // Catch-up strategy: if the peer was here before and the log still covers
    // everything it missed, replay those ops. Otherwise it needs a snapshot.
    const lastSeq = Number.isInteger(msg.lastSeq) ? msg.lastSeq : null;
    if (lastSeq !== null && lastSeq >= room.logStart - 1 && lastSeq <= room.seq) {
      for (const entry of room.log) if (entry.seq > lastSeq) send(ws, entry);
      send(ws, { t: 'caught_up', seq: room.seq });
    } else if (!isHost) {
      requestSnapshot(room, peer);
    }
    broadcastPeers(room);
  }

  function appendOp(room, entry) {
    room.log.push(entry);
    if (room.log.length > OP_LOG_LIMIT) {
      room.log.splice(0, room.log.length - OP_LOG_LIMIT);
      room.logStart = room.log[0].seq;
    }
  }

  function handleMessage(ws, peer, msg) {
    if (msg.t === 'hello') return handleHello(ws, peer, msg);
    if (msg.t === 'ping') return send(ws, { t: 'pong', ts: msg.ts });
    const room = peer.room;
    if (!room) return send(ws, { t: 'error', code: 'not_joined', message: 'Not in a session yet.' });
    room.lastActive = Date.now();

    switch (msg.t) {
      case 'op': {
        room.seq += 1;
        const entry = { t: 'op', seq: room.seq, from: peer.id, id: msg.id, op: msg.op };
        appendOp(room, entry);
        const raw = JSON.stringify(entry);
        for (const p of room.peers.values()) if (p.ws.readyState === 1) p.ws.send(raw);
        break;
      }
      case 'latency':
        peer.latency = Math.max(0, Math.round(Number(msg.ms) || 0));
        break;
      case 'snapshot': {
        if (peer.id !== room.hostId) break;
        const targets = msg.for ? [msg.for] : [...room.pendingSnapshots];
        const seq = Number.isInteger(msg.seq) ? msg.seq : room.seq;
        for (const id of targets) {
          const p = room.peers.get(id);
          room.pendingSnapshots.delete(id);
          if (!p) continue;
          send(p.ws, { t: 'snapshot', seq, state: msg.state, from: peer.id });
          // Ops sequenced after the host took the snapshot still need to reach this peer.
          for (const entry of room.log) if (entry.seq > seq) send(p.ws, entry);
        }
        break;
      }
      case 'resync':
        if (msg.all && peer.id === room.hostId) {
          for (const p of room.peers.values()) if (p.id !== peer.id) requestSnapshot(room, p);
        } else {
          requestSnapshot(room, peer);
        }
        break;
      case 'hash': {
        peer.lastHash = { seq: msg.seq, hash: msg.hash };
        for (const p of room.peers.values()) {
          if (p.id !== peer.id) send(p.ws, { t: 'hash', seq: msg.seq, hash: msg.hash, from: peer.id, host: peer.id === room.hostId });
        }
        break;
      }
      case 'file_has':
        send(ws, { t: 'file_has', hash: msg.hash, present: fileExists(msg.hash) });
        break;
      case 'file_put':
        handleFilePut(room, peer, msg);
        break;
      case 'file_get':
        handleFileGet(ws, msg);
        break;
      case 'chat':
        for (const p of room.peers.values()) send(p.ws, { t: 'chat', from: peer.id, name: peer.name, text: String(msg.text || '').slice(0, 500) });
        break;
      default:
        send(ws, { t: 'error', code: 'unknown', message: `Unknown message ${msg.t}` });
    }
  }

  const safeHash = (h) => /^[a-f0-9]{16,64}$/.test(String(h || ''));
  const filePath = (h) => path.join(filesDir, h);
  const fileExists = (h) => safeHash(h) && fs.existsSync(filePath(h));

  function handleFilePut(room, peer, msg) {
    const { hash, chunk, total } = msg;
    if (!safeHash(hash) || !Number.isInteger(chunk) || !Number.isInteger(total) || total < 1) return;
    if (fileExists(hash)) {
      send(peer.ws, { t: 'file_done', hash });
      return;
    }
    if ((msg.size || 0) > MAX_FILE_MB * 1024 * 1024) {
      send(peer.ws, { t: 'error', code: 'file_too_big', message: `Audio file is larger than ${MAX_FILE_MB} MB and was not shared.` });
      return;
    }
    let up = uploads.get(hash);
    if (!up) {
      const tmp = filePath(hash) + '.part.' + peer.id;
      up = { tmp, fd: fs.openSync(tmp, 'w'), received: new Set(), total, chunkSize: msg.chunkSize || 262144, name: msg.name };
      uploads.set(hash, up);
    }
    const buf = Buffer.from(String(msg.data || ''), 'base64');
    fs.writeSync(up.fd, buf, 0, buf.length, chunk * up.chunkSize);
    up.received.add(chunk);
    if (up.received.size === up.total) {
      fs.closeSync(up.fd);
      const data = fs.readFileSync(up.tmp);
      const actual = crypto.createHash('sha1').update(data).digest('hex');
      uploads.delete(hash);
      if (!actual.startsWith(hash) && actual !== hash) {
        fs.unlinkSync(up.tmp);
        send(peer.ws, { t: 'error', code: 'file_corrupt', message: 'An audio file got corrupted on the way. It will be re-sent.' });
        return;
      }
      fs.renameSync(up.tmp, filePath(hash));
      room.files.add(hash);
      send(peer.ws, { t: 'file_done', hash });
      for (const p of room.peers.values()) if (p.id !== peer.id) send(p.ws, { t: 'file_available', hash });
    }
  }

  function handleFileGet(ws, msg) {
    const hash = msg.hash;
    if (!fileExists(hash)) return send(ws, { t: 'file_missing', hash });
    const chunkSize = 262144;
    const data = fs.readFileSync(filePath(hash));
    const total = Math.max(1, Math.ceil(data.length / chunkSize));
    let i = 0;
    // Pace the transfer so ops keep flowing while big files download.
    const pump = () => {
      while (i < total && ws.readyState === 1 && ws.bufferedAmount < 2 * 1024 * 1024) {
        const slice = data.subarray(i * chunkSize, (i + 1) * chunkSize);
        send(ws, { t: 'file_chunk', hash, chunk: i, total, size: data.length, chunkSize, data: slice.toString('base64') });
        i += 1;
      }
      if (i < total && ws.readyState === 1) setTimeout(pump, 20);
    };
    pump();
  }

  wss.on('connection', (ws) => {
    const peer = { ws, id: null, room: null, latency: null, joinedAt: Date.now() };
    ws.isAlive = true;
    ws.on('pong', () => { ws.isAlive = true; });
    ws.on('message', (data) => {
      let msg;
      try { msg = JSON.parse(data.toString()); } catch (e) {
        return send(ws, { t: 'error', code: 'bad_json', message: 'Malformed message' });
      }
      try { handleMessage(ws, peer, msg); } catch (e) {
        log('error handling', msg && msg.t, e);
        send(ws, { t: 'error', code: 'server', message: 'The server hit a problem with that change.' });
      }
    });
    ws.on('close', () => {
      const room = peer.room;
      if (!room || !room.peers.has(peer.id) || room.peers.get(peer.id) !== peer) return;
      room.peers.delete(peer.id);
      room.pendingSnapshots.delete(peer.id);
      log('peer', peer.id, 'left', room.code);
      if (room.hostId === peer.id) {
        pickHost(room);
        // Anyone waiting for a snapshot from the old host asks the new one.
        for (const id of [...room.pendingSnapshots]) {
          const p = room.peers.get(id);
          room.pendingSnapshots.delete(id);
          if (p) requestSnapshot(room, p);
        }
      }
      broadcastPeers(room);
    });
  });

  const heartbeat = setInterval(() => {
    for (const ws of wss.clients) {
      if (!ws.isAlive) { ws.terminate(); continue; }
      ws.isAlive = false;
      try { ws.ping(); } catch (e) { /* ignore */ }
    }
    const now = Date.now();
    for (const [code, room] of rooms) {
      if (room.peers.size === 0 && now - room.lastActive > ROOM_IDLE_MS) {
        rooms.delete(code);
        log('room expired', code);
      }
    }
  }, 15000);
  heartbeat.unref();

  return {
    httpServer, wss, rooms,
    listen(port, host, cb) { httpServer.listen(port, host, cb); return this; },
    close(cb) { clearInterval(heartbeat); for (const c of wss.clients) c.terminate(); wss.close(); httpServer.close(cb); },
  };
}

module.exports = { createServer, PROTOCOL_VERSION };

if (require.main === module) {
  const port = parseInt(process.env.PORT || '8787', 10);
  createServer().listen(port, '0.0.0.0', () => console.log(`LiveSync relay listening on :${port}`));
}
