// SPDX-License-Identifier: Apache-2.0
// server.js — the local helper the extension dials.
//
// Binds 127.0.0.1 only. Two surfaces:
//   /ws    the extension's WebSocket (one at a time, token-authenticated)
//   HTTP   local clients: POST /run (SSE stream), POST /runs/:id/cancel,
//          POST /runs/:id/gate, GET /health
//
// Threat model in one line: anything that can reach this port is already a
// local process, so the token is what separates "the user's CLI" from "some
// other program that guessed 8787", and the Origin check is what stops a web
// page in the user's own browser from driving it.

import http from "node:http";
import crypto from "node:crypto";
import { isUpgrade, acceptKey, WsConnection } from "./ws.js";

const DEFAULT_PORT = 8787;

export function createBridgeServer({ token, port = DEFAULT_PORT, log = console.error } = {}) {
  if (!token) throw new Error("a token is required — generate one in the extension's Settings");

  let ext = null;               // the authenticated extension connection
  let extAuthed = false;
  const clients = new Map();    // runId -> { res, closed } — SSE listeners
  const waiting = new Map();    // runId -> resolve, for the initial accept/reject

  const hmac = (nonce) => crypto.createHmac("sha256", token).update(String(nonce)).digest("hex");

  function sendToExt(frame) {
    if (!ext || !extAuthed) return false;
    return ext.send(JSON.stringify(frame));
  }

  // ---------- the extension socket ----------

  function attachExtension(conn) {
    if (ext) { conn.close(4003); return; } // one browser at a time
    ext = conn;
    extAuthed = false;
    let helperNonce = null;

    conn.on("message", (text) => {
      let frame;
      try { frame = JSON.parse(text); } catch { return; }

      if (!extAuthed) {
        if (frame.type === "hello") {
          // Prove we hold the token against THEIR nonce, and challenge them
          // with ours. Neither side ever sends the token itself.
          helperNonce = crypto.randomUUID();
          conn.send(JSON.stringify({ type: "hello_ack", proof: hmac(frame.nonce), nonce: helperNonce }));
          return;
        }
        if (frame.type === "hello_proof") {
          const expected = hmac(helperNonce);
          const got = String(frame.proof || "");
          const ok = got.length === expected.length &&
            crypto.timingSafeEqual(Buffer.from(got), Buffer.from(expected));
          if (!ok) { log("[bridge] extension failed token check"); conn.close(4001); return; }
          extAuthed = true;
          conn.send(JSON.stringify({ type: "ready" }));
          log("[bridge] extension connected");
          return;
        }
        conn.close(4001);
        return;
      }

      if (frame.type === "ping") { conn.send(JSON.stringify({ type: "pong" })); return; }
      if (frame.type === "pong") return;
      if (frame.type === "event") routeEvent(frame);
    });

    conn.on("close", () => {
      if (ext === conn) { ext = null; extAuthed = false; log("[bridge] extension disconnected"); }
      // Every in-flight run just lost its executor.
      for (const [id, client] of clients) {
        writeEvent(client, { event: "error", error: "disconnected", detail: "the extension disconnected" });
        endClient(id);
      }
      for (const [id, resolve] of waiting) { resolve({ ok: false, error: "disconnected" }); waiting.delete(id); }
    });
  }

  function routeEvent(frame) {
    const { id } = frame;
    const pending = waiting.get(id);
    if (pending) {
      // The first frame decides whether the run was taken up at all.
      if (frame.event === "accepted") { waiting.delete(id); pending({ ok: true }); }
      else if (frame.event === "error") { waiting.delete(id); pending({ ok: false, error: frame.error, detail: frame.detail }); return; }
    }
    const client = clients.get(id);
    if (!client) return;
    writeEvent(client, frame);
    if (frame.event === "done" || frame.event === "error") endClient(id);
  }

  // ---------- HTTP for local clients ----------

  // Liveness is read off the RESPONSE, never the request: a POST's request
  // stream closes the moment its body is read, which is long before the client
  // goes away — keying off it silently swallowed every later event.
  function writeEvent(client, frame) {
    const { res } = client;
    if (client.closed || res.writableEnded || res.destroyed) return;
    try { res.write(`data: ${JSON.stringify(frame)}\n\n`); } catch { client.closed = true; }
  }

  function endClient(id) {
    const client = clients.get(id);
    if (!client) return;
    clients.delete(id);
    try { if (!client.res.writableEnded) client.res.end(); } catch {}
  }

  function authorized(req) {
    // A browser page can't set a custom Authorization header cross-origin
    // without a preflight we never answer — but a same-site page could still
    // POST a simple request, so reject anything carrying an Origin outright.
    if (req.headers.origin) return false;
    const auth = String(req.headers.authorization || "");
    const presented = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (presented.length !== token.length) return false;
    return crypto.timingSafeEqual(Buffer.from(presented), Buffer.from(token));
  }

  const readBody = (req) => new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > 8 * 1024 * 1024) { reject(new Error("request body too large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {}); }
      catch (e) { reject(e); }
    });
    req.on("error", reject);
  });

  const json = (res, code, body) => {
    res.writeHead(code, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");

    if (req.method === "GET" && url.pathname === "/health") {
      return json(res, 200, { ok: true, extension: extAuthed ? "connected" : "disconnected" });
    }

    if (!authorized(req)) return json(res, 401, { ok: false, error: "unauthorized" });

    if (req.method === "POST" && url.pathname === "/run") {
      if (!extAuthed) return json(res, 503, { ok: false, error: "extension not connected" });
      let task;
      try { task = await readBody(req); } catch (e) { return json(res, 400, { ok: false, error: String(e.message || e) }); }

      const id = crypto.randomUUID();
      res.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "keep-alive",
      });
      const client = { res, closed: false };
      clients.set(id, client);
      writeEvent(client, { event: "queued", id });
      // The client hung up (Ctrl-C). Stop the run rather than letting the
      // browser keep working for nobody.
      res.on("close", () => {
        client.closed = true;
        if (clients.get(id) === client) { clients.delete(id); sendToExt({ type: "cancel", id }); }
      });

      const accepted = new Promise((resolve) => waiting.set(id, resolve));
      sendToExt({ type: "run", id, task });
      const verdict = await accepted;
      if (!verdict.ok) {
        writeEvent(client, { event: "error", error: verdict.error || "rejected", detail: verdict.detail || "" });
        endClient(id);
      }
      return;
    }

    let m = url.pathname.match(/^\/runs\/([\w-]+)\/cancel$/);
    if (req.method === "POST" && m) {
      sendToExt({ type: "cancel", id: m[1] });
      return json(res, 200, { ok: true });
    }

    m = url.pathname.match(/^\/runs\/([\w-]+)\/gate$/);
    if (req.method === "POST" && m) {
      let body;
      try { body = await readBody(req); } catch { body = {}; }
      sendToExt({ type: "gate_reply", id: m[1], gateId: body.gateId, decision: body.decision === "approve" ? "approve" : "cancel" });
      return json(res, 200, { ok: true });
    }

    json(res, 404, { ok: false, error: "not found" });
  });

  server.on("upgrade", (req, socket, head) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (url.pathname !== "/ws" || !isUpgrade(req)) { socket.destroy(); return; }
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\n" +
      "Upgrade: websocket\r\n" +
      "Connection: Upgrade\r\n" +
      `Sec-WebSocket-Accept: ${acceptKey(req.headers["sec-websocket-key"])}\r\n\r\n`,
    );
    const conn = new WsConnection(socket);
    if (head?.length) socket.emit("data", head);
    attachExtension(conn);
  });

  return {
    server,
    listen: () => new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve(server.address()))),
    close: () => new Promise((resolve) => { ext?.close(1001); server.close(resolve); }),
    get extensionConnected() { return extAuthed; },
  };
}

// Run directly: `node server.js --token <token> [--port 8787]`
if (import.meta.url === `file://${process.argv[1]}`) {
  const args = process.argv.slice(2);
  const arg = (name, fallback) => {
    const i = args.indexOf(`--${name}`);
    return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
  };
  const token = arg("token", process.env.AINXT_TOKEN);
  const port = Number(arg("port", process.env.AINXT_PORT || DEFAULT_PORT));
  if (!token) {
    console.error("usage: node server.js --token <token> [--port 8787]");
    console.error("the token is generated in the extension: Settings → Local command bridge");
    process.exit(2);
  }
  const bridge = createBridgeServer({ token, port });
  bridge.listen().then((addr) => {
    console.error(`[bridge] listening on http://127.0.0.1:${addr.port} (loopback only)`);
    console.error("[bridge] waiting for the extension to connect…");
  });
}
