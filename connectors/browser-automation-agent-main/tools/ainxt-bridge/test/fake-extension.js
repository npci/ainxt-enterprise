// SPDX-License-Identifier: Apache-2.0
// fake-extension.js — a scriptable stand-in for the browser side.
//
// Speaks the same handshake lib/bridge.js does (client role: masked frames out,
// verify the helper's proof, answer the challenge) so the tests exercise the
// real server code rather than a mock of it.

import net from "node:net";
import crypto from "node:crypto";

const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

export function connectFakeExtension({ port, token, onFrame }) {
  return new Promise((resolve, reject) => {
    const key = crypto.randomBytes(16).toString("base64");
    const socket = net.connect(port, "127.0.0.1");
    let handshakeDone = false;
    let buf = Buffer.alloc(0);
    let authed = false;
    const extNonce = crypto.randomUUID();
    const hmac = (nonce) => crypto.createHmac("sha256", token).update(String(nonce)).digest("hex");
    const events = { ready: null, closed: false };

    const api = {
      send: (obj) => socket.write(encodeMaskedText(JSON.stringify(obj))),
      close: () => socket.destroy(),
      get authed() { return authed; },
      get closed() { return events.closed; },
    };

    socket.on("error", reject);
    socket.on("close", () => { events.closed = true; });

    socket.on("connect", () => {
      socket.write(
        `GET /ws HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`,
      );
    });

    socket.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (!handshakeDone) {
        const end = buf.indexOf("\r\n\r\n");
        if (end < 0) return;
        const head = buf.subarray(0, end).toString();
        if (!/101/.test(head)) return reject(new Error(`upgrade refused: ${head.split("\r\n")[0]}`));
        buf = buf.subarray(end + 4);
        handshakeDone = true;
        api.send({ type: "hello", role: "extension", extensionId: "fake", version: "test", nonce: extNonce });
      }

      for (;;) {
        const frame = decodeUnmasked(buf);
        if (!frame) return;
        buf = buf.subarray(frame.size);
        if (frame.opcode === 0x8) { socket.destroy(); return; }
        if (frame.opcode !== 0x1) continue;
        let msg;
        try { msg = JSON.parse(frame.payload.toString("utf8")); } catch { continue; }

        if (!authed) {
          if (msg.type === "hello_ack") {
            if (msg.proof !== hmac(extNonce)) { socket.destroy(); return reject(new Error("helper failed our challenge")); }
            api.send({ type: "hello_proof", proof: hmac(msg.nonce) });
            continue;
          }
          if (msg.type === "ready") { authed = true; resolve(api); continue; }
          continue;
        }
        onFrame?.(msg, api);
      }
    });
  });
}

// Client frames must be masked (RFC 6455 §5.3).
function encodeMaskedText(text) {
  const payload = Buffer.from(text, "utf8");
  const mask = crypto.randomBytes(4);
  const len = payload.length;
  let header;
  if (len < 126) { header = Buffer.alloc(2); header[1] = 0x80 | len; }
  else if (len < 65536) { header = Buffer.alloc(4); header[1] = 0x80 | 126; header.writeUInt16BE(len, 2); }
  else { header = Buffer.alloc(10); header[1] = 0x80 | 127; header.writeBigUInt64BE(BigInt(len), 2); }
  header[0] = 0x81; // FIN + text
  const masked = Buffer.allocUnsafe(len);
  for (let i = 0; i < len; i++) masked[i] = payload[i] ^ mask[i & 3];
  return Buffer.concat([header, mask, masked]);
}

function decodeUnmasked(buf) {
  if (buf.length < 2) return null;
  const fin = (buf[0] & 0x80) !== 0;
  const opcode = buf[0] & 0x0f;
  let len = buf[1] & 0x7f;
  let offset = 2;
  if (len === 126) { if (buf.length < 4) return null; len = buf.readUInt16BE(2); offset = 4; }
  else if (len === 127) { if (buf.length < 10) return null; len = Number(buf.readBigUInt64BE(2)); offset = 10; }
  if (buf.length < offset + len) return null;
  return { fin, opcode, payload: buf.subarray(offset, offset + len), size: offset + len };
}
