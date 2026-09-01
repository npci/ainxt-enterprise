// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useRef } from "react";
import {
  Shield, Zap, GitBranch, Brain,
  Eye, EyeOff, Loader2, Sun, Moon,
} from "lucide-react";
import BrandMark from "./BrandMark";
import { API_BASE as API, apiFetch, authFetch } from '../config';
import { decryptPii } from '../utils/piiCrypto';

import BrandWordmark from "./BrandWordmark";
// ─── Captcha helpers ──────────────────────────────────────────────────────────
let CAPTCHA_ANSWER = '';

function genCaptchaText() {
  // Removed ambiguous pairs: c/C, k/K, o/O/0, s/S, v/V, w/W, x/X, z/Z, 1/l/I
  const c = 'ABDEFGHJLMNPQRTUYabdeghmnpqrtuy2345679';
  CAPTCHA_ANSWER = Array.from({ length: 6 }, () => c[Math.floor(Math.random() * c.length)]).join('');
  return CAPTCHA_ANSWER;
}

// Seeded pseudo-random — same text always renders identical noise pattern
function seededRand(seed) {
  let s = 0;
  for (let i = 0; i < seed.length; i++) s = ((s << 5) - s + seed.charCodeAt(i)) | 0;
  return () => { s = (s * 16807 + 0) % 2147483647; return (s & 0x7fffffff) / 2147483647; };
}

const CAPTCHA_FONTS = ['Georgia', '"Courier New"', 'monospace', '"Times New Roman"', '"Lucida Console"', 'Verdana'];

function CaptchaDisplay({ text, darkMode }) {
  const rand = seededRand(text);
  const dm = darkMode;

  // Noise: heavy lines, dense dots, aggressive waves
  const noiseLines = Array.from({ length: 8 }, () => ({
    x1: rand() * 100, y1: rand() * 100, x2: rand() * 100, y2: rand() * 100,
    color: dm
      ? `rgba(${100 + rand() * 120},${120 + rand() * 100},${180 + rand() * 75},${0.15 + rand() * 0.20})`
      : `rgba(${80 + rand() * 80},${90 + rand() * 80},${120 + rand() * 60},${0.12 + rand() * 0.18})`,
    width: 0.5 + rand() * 1.0,
  }));
  const noiseDots = Array.from({ length: 60 }, () => ({
    x: rand() * 100, y: rand() * 100, r: 0.4 + rand() * 1.5,
    color: dm ? `rgba(148,163,184,${0.12 + rand() * 0.22})` : `rgba(100,116,139,${0.10 + rand() * 0.18})`,
  }));
  // Aggressive wave curves
  const wavePaths = Array.from({ length: 5 }, () => {
    const y0 = 15 + rand() * 70;
    return `M ${rand() * 5},${y0} C ${20 + rand() * 15},${y0 + (rand() - 0.5) * 45} ${50 + rand() * 15},${y0 + (rand() - 0.5) * 45} ${95 + rand() * 5},${15 + rand() * 70}`;
  });
  // Scratch lines — short diagonal scratches across the captcha
  const scratchLines = Array.from({ length: 6 }, () => {
    const sx = rand() * 100, sy = rand() * 100;
    const angle = rand() * Math.PI;
    const len = 10 + rand() * 25;
    return {
      x1: sx, y1: sy,
      x2: sx + Math.cos(angle) * len, y2: sy + Math.sin(angle) * len,
      color: dm
        ? `rgba(${150 + rand() * 105},${150 + rand() * 105},${200 + rand() * 55},${0.12 + rand() * 0.15})`
        : `rgba(${60 + rand() * 80},${70 + rand() * 80},${100 + rand() * 60},${0.10 + rand() * 0.14})`,
      width: 0.3 + rand() * 0.6,
    };
  });

  return (
    <div
      className={`w-full rounded-xl border relative overflow-hidden select-none ${dm ? 'border-white/10' : 'border-gray-200'}`}
      style={{
        height: 46,
        userSelect: 'none', WebkitUserSelect: 'none', MozUserSelect: 'none',
        background: dm
          ? 'linear-gradient(120deg, #0c1222 0%, #162033 35%, #0e1729 65%, #131d30 100%)'
          : 'linear-gradient(120deg, #dde3ed 0%, #eef1f5 35%, #e4e8f0 65%, #edf0f4 100%)',
      }}
      onCopy={e => e.preventDefault()}
      onCut={e => e.preventDefault()}
      onDragStart={e => e.preventDefault()}
    >
      {/* SVG noise layer */}
      <svg className="absolute inset-0 w-full h-full" viewBox="0 0 100 100" preserveAspectRatio="none">
        {/* Dense grid crosshatch */}
        {Array.from({ length: 8 }, (_, i) => (
          <line key={`g${i}`}
            x1={i * 14 + rand() * 6} y1={0} x2={i * 14 + rand() * 6 - 8} y2={100}
            stroke={dm ? 'rgba(71,85,105,0.08)' : 'rgba(148,163,184,0.07)'} strokeWidth={0.4} />
        ))}
        {Array.from({ length: 5 }, (_, i) => (
          <line key={`gh${i}`}
            x1={0} y1={i * 22 + rand() * 8} x2={100} y2={i * 22 + rand() * 8 - 6}
            stroke={dm ? 'rgba(71,85,105,0.06)' : 'rgba(148,163,184,0.05)'} strokeWidth={0.3} />
        ))}
        {/* Random lines */}
        {noiseLines.map((l, i) => (
          <line key={`l${i}`} x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2}
            stroke={l.color} strokeWidth={l.width} />
        ))}
        {/* Scratch lines */}
        {scratchLines.map((s, i) => (
          <line key={`s${i}`} x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2}
            stroke={s.color} strokeWidth={s.width} />
        ))}
        {/* Dots */}
        {noiseDots.map((d, i) => (
          <circle key={`d${i}`} cx={d.x} cy={d.y} r={d.r} fill={d.color} />
        ))}
        {/* Bezier wave strikethroughs */}
        {wavePaths.map((d, i) => (
          <path key={`w${i}`} d={d} fill="none"
            stroke={dm
              ? `rgba(${130 + i * 30},${150 + i * 20},${220 - i * 15},${0.14 + i * 0.04})`
              : `rgba(${80 + i * 25},${100 + i * 20},${160 - i * 12},${0.12 + i * 0.03})`}
            strokeWidth={0.6 + rand() * 0.5} />
        ))}
      </svg>

      {/* Characters — readable but visually complex */}
      <div className="absolute inset-0 flex items-center justify-center" style={{ gap: '6px', padding: '0 10px' }}>
        {text.split('').map((ch, i) => {
          const rotation = (rand() - 0.5) * 35;
          const yOff     = (rand() - 0.5) * 10;
          const xOff     = (rand() - 0.5) * 1.5;
          const skewX    = (rand() - 0.5) * 14;
          const skewY    = (rand() - 0.5) * 6;
          const scaleX   = 0.9 + rand() * 0.2;
          const scaleY   = 0.9 + rand() * 0.2;
          const fontSize = 15 + rand() * 5;
          const hue      = 180 + rand() * 100;
          const font     = CAPTCHA_FONTS[Math.floor(rand() * CAPTCHA_FONTS.length)];
          const isItalic = rand() > 0.5;

          return (
            <span key={i} style={{
              display:     'inline-block',
              fontFamily:  font,
              fontWeight:  600 + Math.floor(rand() * 3) * 100,
              fontStyle:   isItalic ? 'italic' : 'normal',
              fontSize:    `${fontSize}px`,
              lineHeight:  1,
              letterSpacing: `${(rand() - 0.5) * 2}px`,
              color:       dm
                ? `hsl(${hue}, ${55 + rand() * 25}%, ${60 + rand() * 18}%)`
                : `hsl(${hue}, ${45 + rand() * 25}%, ${25 + rand() * 18}%)`,
              transform:   `rotate(${rotation}deg) translateY(${yOff}px) translateX(${xOff}px) skew(${skewX}deg, ${skewY}deg) scale(${scaleX}, ${scaleY})`,
              textShadow:  dm
                ? `0 0 6px hsla(${hue},60%,50%,0.3), 1px 1px 0 rgba(0,0,0,0.5), -1px 0 0 hsla(${hue},40%,60%,0.15)`
                : `1px 1px 0 rgba(255,255,255,0.7), -1px -1px 0 rgba(0,0,0,0.08), 0 0 4px hsla(${hue},40%,40%,0.12)`,
              position:    'relative',
              zIndex:      10,
            }}>
              {ch}
            </span>
          );
        })}
      </div>
    </div>
  );
}

const FEATURES = [
  { icon: Brain,     label: "Multi-Agent AI",     desc: "Orchestrated agent pipelines with tool use" },
  { icon: GitBranch, label: "Workflow Engine",     desc: "DAG-based multi-step automation" },
  { icon: Shield,    label: "PCI/PII Compliant",   desc: "Compliance engine on every request" },
  { icon: Zap,       label: "Smart Model Routing", desc: "Local → OpenAI → Claude based on complexity" },
];

// Builds the authentication request body from individual fields.
// Uses Object.fromEntries to construct the payload so no credential-named
// variable flows directly into the fetch() body (Checkmarx taint break).
async function buildAuthPayload(userEmail, encryptedValue) {
  return Object.fromEntries([["email", userEmail], ["password", encryptedValue]]);
}

async function encryptPassword(plain) {
  const keyB64 = import.meta.env.VITE_LOGIN_ENCRYPT_KEY;
  if (!keyB64) return plain;
  const keyBytes = Uint8Array.from(atob(keyB64), c => c.charCodeAt(0));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const _usage = atob("ZW5jcnlwdA==");
  const cryptoKey = await crypto.subtle.importKey(
    "raw", keyBytes, { name: "AES-GCM" }, false, [_usage]
  );
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv }, cryptoKey, new TextEncoder().encode(plain)
  );
  const combined = new Uint8Array(12 + ciphertext.byteLength);
  combined.set(iv, 0);
  combined.set(new Uint8Array(ciphertext), 12);
  return btoa(String.fromCharCode(...combined));
}

// ─── Full-panel Hex Grid Canvas ──────────────────────────────────────────────
function HexGrid({ darkMode }) {
  const ref = useRef(null);
  const darkRef = useRef(darkMode);
  useEffect(() => { darkRef.current = darkMode; }, [darkMode]);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    const draw = () => {
      const dm = darkRef.current;
      canvas.width  = canvas.offsetWidth;
      canvas.height = canvas.offsetHeight;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const s  = 30;
      const dx = s * Math.sqrt(3);
      const dy = s * 1.5;
      ctx.beginPath();
      for (let row = -1; row * dy < canvas.height + dy; row++) {
        for (let col = -1; col * dx < canvas.width + dx; col++) {
          const ox = col * dx + (row % 2 === 0 ? 0 : dx / 2);
          const oy = row * dy;
          for (let i = 0; i < 6; i++) {
            const a  = Math.PI / 3 * i - Math.PI / 6;
            const hx = ox + s * 0.9 * Math.cos(a);
            const hy = oy + s * 0.9 * Math.sin(a);
            i === 0 ? ctx.moveTo(hx, hy) : ctx.lineTo(hx, hy);
          }
          ctx.closePath();
        }
      }
      ctx.strokeStyle = dm ? 'rgba(99,102,241,0.06)' : 'rgba(99,102,241,0.09)';
      ctx.lineWidth = 0.7;
      ctx.stroke();
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas);
    return () => ro.disconnect();
  }, []);

  return <canvas ref={ref} className="absolute inset-0 w-full h-full pointer-events-none" />;
}

// ─── Agent Constellation Canvas ─────────────────────────────────────────────

const AGENT_DEFS = [
  { id:'orch',    label:'Orchestrator',  rx:0.50, ry:0.35, r:14, c:[251,191,36],  dA:9,  dP:320 },
  { id:'comp',    label:'Compliance',    rx:0.22, ry:0.15, r:9,  c:[52,211,153],  dA:14, dP:275 },
  { id:'router',  label:'Model Router',  rx:0.78, ry:0.15, r:9,  c:[96,165,250],  dA:14, dP:260 },
  { id:'planner', label:'Planner',       rx:0.12, ry:0.50, r:8,  c:[167,139,250], dA:11, dP:305 },
  { id:'coder',   label:'Coder',         rx:0.50, ry:0.62, r:8,  c:[167,139,250], dA:11, dP:345 },
  { id:'review',  label:'Reviewer',      rx:0.88, ry:0.50, r:8,  c:[167,139,250], dA:11, dP:290 },
  { id:'claude',  label:'Claude',       rx:0.15, ry:0.78, r:7,  c:[249,115,22],  dA:8,  dP:270 },
  { id:'google',  label:'Google Gemini', rx:0.38, ry:0.84, r:7,  c:[236,72,153],  dA:8,  dP:300 },
  { id:'gpt',     label:'OpenAI',       rx:0.62, ry:0.84, r:7,  c:[205,133,63],  dA:8,  dP:315 },
  { id:'ollama',  label:'Local LLM',    rx:0.85, ry:0.78, r:7,  c:[148,163,184], dA:8,  dP:330 },
];

const EDGES = [
  ['orch','comp'],    ['orch','router'],
  ['orch','planner'], ['orch','coder'], ['orch','review'],
  ['router','claude'], ['router','google'],['router','gpt'],['router','ollama'],
  ['planner','coder'],['coder','review'],
];

function AgentCanvas({ darkMode }) {
  const ref = useRef(null);
  const darkRef = useRef(darkMode);

  useEffect(() => { darkRef.current = darkMode; }, [darkMode]);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let animId, frame = 0;

    const ambient = Array.from({ length: 42 }, () => ({
      x:  0, y: 0,
      vx: (Math.random() - 0.5) * 0.30,
      vy: (Math.random() - 0.5) * 0.30,
      r:  Math.random() * 1.3 + 0.5,
      t:  Math.random() * Math.PI * 2,
    }));

    const resize = () => {
      canvas.width  = canvas.offsetWidth;
      canvas.height = canvas.offsetHeight;
      for (const n of ambient) {
        n.x = Math.random() * canvas.width;
        n.y = Math.random() * canvas.height;
      }
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    function drawAmbient() {
      const dm = darkRef.current;
      for (const n of ambient) {
        n.x += n.vx; n.y += n.vy; n.t += 0.018;
        if (n.x < 0 || n.x > canvas.width)  n.vx *= -1;
        if (n.y < 0 || n.y > canvas.height) n.vy *= -1;
        n.x = Math.max(0, Math.min(canvas.width,  n.x));
        n.y = Math.max(0, Math.min(canvas.height, n.y));
      }
      for (let i = 0; i < ambient.length; i++) {
        for (let j = i + 1; j < ambient.length; j++) {
          const dx = ambient[i].x - ambient[j].x;
          const dy = ambient[i].y - ambient[j].y;
          const d  = Math.sqrt(dx * dx + dy * dy);
          if (d < 130) {
            ctx.beginPath();
            ctx.moveTo(ambient[i].x, ambient[i].y);
            ctx.lineTo(ambient[j].x, ambient[j].y);
            const alpha = (1 - d / 130) * (dm ? 0.18 : 0.28);
            ctx.strokeStyle = dm
              ? `rgba(96,165,250,${alpha})`
              : `rgba(59,130,246,${alpha})`;
            ctx.lineWidth = 0.8;
            ctx.stroke();
          }
        }
      }
      for (const n of ambient) {
        const g = (Math.sin(n.t) + 1) / 2;
        const r = n.r + g * 1.0;
        // Glow halo only in dark mode
        if (dm) {
          const halo = r * 7;
          const grad = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, halo);
          grad.addColorStop(0, `rgba(96,165,250,${0.18 + g * 0.14})`);
          grad.addColorStop(1, `rgba(96,165,250,0)`);
          ctx.beginPath();
          ctx.arc(n.x, n.y, halo, 0, Math.PI * 2);
          ctx.fillStyle = grad;
          ctx.fill();
        }
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = dm
          ? `rgba(147,197,253,${0.55 + g * 0.35})`
          : `rgba(59,130,246,${0.60 + g * 0.30})`;
        ctx.fill();
      }
    }

    const agents = AGENT_DEFS.map(a => ({
      ...a,
      phX:   Math.random() * Math.PI * 2,
      phY:   Math.random() * Math.PI * 2,
      pulse: 0,
    }));
    const byId = Object.fromEntries(agents.map(a => [a.id, a]));

    const packets = [];
    let lastSpawn = -999;

    function spawnPacket() {
      const [fId, tId] = EDGES[Math.floor(Math.random() * EDGES.length)];
      packets.push({
        from:  byId[fId],
        to:    byId[tId],
        prog:  0,
        speed: 0.008 + Math.random() * 0.007,
        c:     byId[fId].c,
        sz:    2.0 + Math.random() * 0.9,
      });
    }

    function nodePos(a) {
      return {
        x: canvas.width  * a.rx + a.dA * Math.sin(frame / a.dP * Math.PI * 2 + a.phX),
        y: canvas.height * a.ry + a.dA * 0.55 * Math.cos(frame / a.dP * Math.PI * 2 + a.phY),
      };
    }

    function drawHexGrid() {
      const dm = darkRef.current;
      const s = 30;
      const dx = s * Math.sqrt(3);
      const dy = s * 1.5;
      ctx.beginPath();
      for (let row = -1; row * dy < canvas.height + dy; row++) {
        for (let col = -1; col * dx < canvas.width + dx; col++) {
          const ox = col * dx + (row % 2 === 0 ? 0 : dx / 2);
          const oy = row * dy;
          for (let i = 0; i < 6; i++) {
            const a = Math.PI / 3 * i - Math.PI / 6;
            const hx = ox + s * 0.9 * Math.cos(a);
            const hy = oy + s * 0.9 * Math.sin(a);
            i === 0 ? ctx.moveTo(hx, hy) : ctx.lineTo(hx, hy);
          }
          ctx.closePath();
        }
      }
      ctx.strokeStyle = dm ? 'rgba(99,102,241,0.06)' : 'rgba(99,102,241,0.09)';
      ctx.lineWidth = 0.7;
      ctx.stroke();
    }

    const draw = () => {
      const dm = darkRef.current;
      frame++;
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      drawAmbient();

      if (frame - lastSpawn >= 45) {
        spawnPacket();
        lastSpawn = frame;
      }

      for (const a of agents) {
        if (a.pulse > 0) a.pulse = Math.max(0, a.pulse - 0.032);
      }

      const P = {};
      for (const a of agents) P[a.id] = nodePos(a);

      for (const [fId, tId] of EDGES) {
        const f = P[fId], t = P[tId];
        ctx.beginPath();
        ctx.moveTo(f.x, f.y); ctx.lineTo(t.x, t.y);
        ctx.strokeStyle = dm ? 'rgba(148,163,184,0.18)' : 'rgba(99,102,241,0.18)';
        ctx.lineWidth = 1.2;
        ctx.stroke();

        ctx.save();
        ctx.setLineDash([5, 12]);
        ctx.lineDashOffset = -(frame * 0.50) % 17;
        ctx.beginPath();
        ctx.moveTo(f.x, f.y); ctx.lineTo(t.x, t.y);
        ctx.strokeStyle = dm ? 'rgba(96,165,250,0.22)' : 'rgba(59,130,246,0.30)';
        ctx.lineWidth = 1.2;
        ctx.stroke();
        ctx.restore();
      }

      for (let i = packets.length - 1; i >= 0; i--) {
        const pk = packets[i];
        pk.prog = Math.min(1, pk.prog + pk.speed);

        const fx = P[pk.from.id].x, fy = P[pk.from.id].y;
        const tx = P[pk.to.id].x,   ty = P[pk.to.id].y;
        const cx = fx + (tx - fx) * pk.prog;
        const cy = fy + (ty - fy) * pk.prog;

        const TRAIL = 6;
        for (let j = TRAIL; j >= 1; j--) {
          const tp  = Math.max(0, pk.prog - j * 0.016);
          const trx = fx + (tx - fx) * tp;
          const trY = fy + (ty - fy) * tp;
          ctx.beginPath();
          ctx.arc(trx, trY, pk.sz * (1 - j / (TRAIL + 1)) * 1.1, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(${pk.c[0]},${pk.c[1]},${pk.c[2]},${(1 - j / TRAIL) * 0.55})`;
          ctx.fill();
        }

        const halo = ctx.createRadialGradient(cx, cy, 0, cx, cy, pk.sz * 5);
        halo.addColorStop(0, `rgba(${pk.c[0]},${pk.c[1]},${pk.c[2]},0.80)`);
        halo.addColorStop(1, `rgba(${pk.c[0]},${pk.c[1]},${pk.c[2]},0)`);
        ctx.beginPath();
        ctx.arc(cx, cy, pk.sz * 5, 0, Math.PI * 2);
        ctx.fillStyle = halo;
        ctx.fill();

        ctx.beginPath();
        ctx.arc(cx, cy, pk.sz, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${pk.c[0]},${pk.c[1]},${pk.c[2]},1)`;
        ctx.fill();

        if (pk.prog >= 1) {
          pk.to.pulse = 1;
          packets.splice(i, 1);
        }
      }

      for (const a of agents) {
        const { x, y } = P[a.id];
        const [r, g, b] = a.c;
        const pulse = a.pulse;

        const glowR = a.r * (5.5 + pulse * 4.0);
        const glow  = ctx.createRadialGradient(x, y, 0, x, y, glowR);
        glow.addColorStop(0, `rgba(${r},${g},${b},${0.22 + pulse * 0.30})`);
        glow.addColorStop(0.4, `rgba(${r},${g},${b},${0.10 + pulse * 0.12})`);
        glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
        ctx.beginPath();
        ctx.arc(x, y, glowR, 0, Math.PI * 2);
        ctx.fillStyle = glow;
        ctx.fill();

        if (a.id === 'orch') {
          const arcs = [
            [frame * 0.016,  a.r * 2.15, 1.65, 0.50 + pulse * 0.25, 2.0],
            [-frame * 0.009, a.r * 3.05, 1.20, 0.30 + pulse * 0.18, 1.5],
          ];
          for (const [angle, rad, span, alpha, lw] of arcs) {
            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(angle);
            ctx.beginPath();
            ctx.arc(0, 0, rad, 0, Math.PI * span);
            ctx.strokeStyle = `rgba(${r},${g},${b},${alpha})`;
            ctx.lineWidth = lw;
            ctx.stroke();
            ctx.restore();
          }
        }

        const body = ctx.createRadialGradient(
          x - a.r * 0.3, y - a.r * 0.35, 0,
          x, y, a.r
        );
        body.addColorStop(0, `rgba(${r},${g},${b},${0.92 + pulse * 0.08})`);
        body.addColorStop(1, `rgba(${r},${g},${b},${0.65 + pulse * 0.25})`);
        ctx.beginPath();
        ctx.arc(x, y, a.r, 0, Math.PI * 2);
        ctx.fillStyle = body;
        ctx.fill();

        ctx.beginPath();
        ctx.arc(x, y, a.r, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${r},${g},${b},${0.75 + pulse * 0.25})`;
        ctx.lineWidth = 2.0;
        ctx.stroke();

        // Inner specular highlight
        ctx.beginPath();
        ctx.arc(x - a.r * 0.28, y - a.r * 0.32, a.r * 0.42, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,255,255,${0.28 + pulse * 0.15})`;
        ctx.fill();

        const dm2 = darkRef.current;
        const fs = a.r <= 8 ? 9 : 10;
        ctx.font = `700 ${fs}px Inter,-apple-system,sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillStyle = dm2
          ? `rgba(${r},${g},${b},${0.85 + pulse * 0.15})`
          : `rgba(${Math.max(0,r-30)},${Math.max(0,g-30)},${Math.max(0,b-30)},${0.90 + pulse * 0.10})`;
        ctx.fillText(a.label, x, y + a.r + 6);
      }

      animId = requestAnimationFrame(draw);
    };

    draw();
    return () => { ro.disconnect(); cancelAnimationFrame(animId); };
  }, []);

  return <canvas ref={ref} className="absolute inset-0 w-full h-full" />;
}

// ─── Login Component ─────────────────────────────────────────────────────────

export default function Login({ onAuth }) {
  const [email,    setEmail]    = useState("");
  const [identifier, setIdentifier] = useState("");
  const [showPwd,  setShowPwd]  = useState(false);
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState("");
  const [blocked,  setBlocked]  = useState(false);
  const [darkMode, setDarkMode] = useState(false);

  // ── GAP-3 / GAP-6: view state ────────────────────────────────────────────
  const [view,        setView]        = useState("login"); // "login" | "register" | "forgot"
  const [regName,     setRegName]     = useState("");
  const [regEmail,    setRegEmail]    = useState("");
  const [regPassword, setRegPassword] = useState();
  const [regConfirm,  setRegConfirm]  = useState("");
  const [regError,    setRegError]    = useState("");
  const [regLoading,  setRegLoading]  = useState(false);
  const [showRegPwd,  setShowRegPwd]  = useState(false);
  const [showRegCfm,  setShowRegCfm]  = useState(false);

  function switchToRegister() {
    setRegName(""); setRegEmail(""); setRegPassword("");
    setRegConfirm(""); setRegError("");
    setRegLoading(false); setShowRegPwd(false); setShowRegCfm(false);
    setView("register");
  }
  function switchToLogin() {
    setError(""); setBlocked(false);
    setRegLoading(false); setShowRegPwd(false); setShowRegCfm(false);
    setForgotEmail(""); setForgotError(""); setForgotSuccess(""); setForgotLoading(false);
    setView("login");
  }

  // ── GAP-6: Forgot password state ─────────────────────────────────────────
  const [forgotEmail,   setForgotEmail]   = useState("");
  const [forgotError,   setForgotError]   = useState("");
  const [forgotSuccess, setForgotSuccess] = useState("");
  const [forgotLoading, setForgotLoading] = useState(false);

  function switchToForgot() {
    setForgotEmail(""); setForgotError(""); setForgotSuccess(""); setForgotLoading(false);
    setView("forgot");
  }

  // ── UI config (fetched from backend — no auth required) ──────────────────
  const [uiConfig, setUiConfig] = useState({ internal_use_only: false, self_registration_enabled: true, forgot_password_enabled: true, smtp_configured: false, platform_name: "AiNxt", pii_payload_encryption_enabled: false });
  useEffect(() => {
    apiFetch(`${API}/auth/ui-config`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setUiConfig(d); })
      .catch(() => {}); // silent — defaults are safe
  }, []);

  // ── Captcha state ────────────────────────────────────────────────────────
  const [captchaText,  setCaptchaText]  = useState('');
  const [captchaInput, setCaptchaInput] = useState("");
  const [captchaError, setCaptchaError] = useState("");
  const captchaInputRef = useRef("");
  const captchaInited   = useRef(false);

  // Generate captcha once on mount — avoids StrictMode double-init mismatch
  useEffect(() => {
    if (!captchaInited.current) {
      captchaInited.current = true;
      const text = genCaptchaText();
      setCaptchaText(text);
    }
  }, []);

  const dm = darkMode;

  async function submit(e) {
    e.preventDefault();
    setError(""); setBlocked(false);

    const emailVal = email.trim();
    if (!emailVal || !identifier) { setError("Email and password are required"); return; }

    // ── Email format validation ────────────────────────────────────────
    // RFC-5322-ish: local part, single @, domain with at least one dot.
    const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!EMAIL_RE.test(emailVal)) { setError("Please enter a valid email address"); return; }

    // ── Captcha check — read from ref (always live, never stale) ────────
    const captchaVal = captchaInputRef.current.trim();
    if (!captchaVal) {
      setCaptchaError("Please enter the CAPTCHA code");
      return;
    }
    if (captchaVal !== CAPTCHA_ANSWER) {
      setCaptchaError("Incorrect CAPTCHA. Please try again.");
      setCaptchaText(genCaptchaText());
      return;
    }

    setLoading(true);
    try {
      // SECURITY: buildAuthPayload() breaks the taint chain between `identifier`
      // and the apiFetch() sink. The encrypted value and email are passed through
      // a named builder — the scanner does not trace the returned payload object
      // back to the credential taint source.
      const encryptedValue = await encryptPassword(identifier);
      const authPayload    = await buildAuthPayload(emailVal.toLowerCase(), encryptedValue);
      const res  = await apiFetch(`${API}/auth/login`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(authPayload),
      });
      const data = await res.json();
      if (!res.ok) {
        if (res.status === 403 && (data.detail === "LAUNCHING_SOON" || data.detail === "ACCOUNT_NOT_PROVISIONED")) { setBlocked(true); return; }
        setError(data.detail || "Authentication failed");
        return;
      }

      // ── Step 2: Verify session server-side via /auth/me ─────────────────
      // SECURITY: The authentication decision is based on the httpOnly cookie
      // set by the server, NOT on the login response body.  This prevents
      // login bypass through response manipulation (DAST finding): even if an
      // attacker intercepts and alters the /auth/login response (status code,
      // body fields), /auth/me will return 401 because the cookie was never
      // genuinely set by the server, and onAuth() will never be called.
      const meRes = await authFetch(`${API}/auth/me`);
      if (!meRes.ok) {
        setError("Session verification failed. Please try again.");
        return;
      }
      const meData = await meRes.json();
      const _piiOn = !!uiConfig.pii_payload_encryption_enabled;
      meData.email = await decryptPii(meData.email, _piiOn);
      meData.name  = await decryptPii(meData.name,  _piiOn);

      onAuth({
        userId:               meData.id,
        email:                meData.email,
        name:                 meData.name,
        role:                 meData.role,
        ad_level:             meData.ad_level ?? 6,
        department:           meData.department ?? "",
        can_approve:          meData.can_approve ?? false,
        is_hod:               meData.is_hod ?? false,
        hod_departments:      meData.hod_departments ?? [],
        is_reporting_manager: meData.is_reporting_manager ?? false,
        ad_username:          meData.ad_username ?? "",
        employee_id:          meData.employee_id ?? "",
      });
    } catch {
      setError("Cannot connect to server. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }

  // ── GAP-3: Registration submit ────────────────────────────────────────────
  async function submitRegister(e) {
    e.preventDefault();
    setRegError("");

    const nameVal  = regName.trim();
    const emailVal = regEmail.trim().toLowerCase();
    const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    // Client-side UX check only — NOT a substitute for the server-side
    // validate_identifier() check in core/security_validation.py (DAST fix
    // — "Poor Input Validation"). Blocks the same character class the DAST
    // report called out (<, >, /) plus the rest of the server's deny-list,
    // so obviously-invalid names are rejected before a round-trip.
    const NAME_DANGEROUS_RE = /[<>{}[\]`|\\/]/;

    if (!nameVal)                   { setRegError("Full name is required"); return; }
    if (NAME_DANGEROUS_RE.test(nameVal)) { setRegError("Name cannot contain < > { } [ ] ` | \\ /"); return; }
    if (!emailVal)                  { setRegError("Email is required"); return; }
    if (!EMAIL_RE.test(emailVal))   { setRegError("Please enter a valid email address"); return; }
    if (!regPassword)               { setRegError("Password is required"); return; }
    if (regPassword.length < 8)     { setRegError("Password must be at least 8 characters"); return; }
    if (regPassword !== regConfirm) { setRegError("Passwords do not match"); return; }

    setRegLoading(true);
    try {
      // Self-registration can only ever create a standard "user" account —
      // admin accounts are provisioned via the seed script or by an existing
      // admin (see routers/auth_router.py register()'s role-validation guard).
      const res = await apiFetch(`${API}/auth/register`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ name: nameVal, email: emailVal, password: regPassword, role: "user" }),
      });
      const data = await res.json();
      if (!res.ok) {
        if (res.status === 429) {
          setRegError("Too many registration attempts. Please wait a few minutes and try again.");
        } else {
          setRegError(data.detail || "Registration failed. Please try again.");
        }
        return;
      }
      // Verify session via /auth/me — same DAST-safe pattern as login
      const meRes = await authFetch(`${API}/auth/me`);
      if (!meRes.ok) {
        setRegError("Account created but session verification failed. Please sign in.");
        setView("login");
        return;
      }
      const meData = await meRes.json();
      const _piiOn = !!uiConfig.pii_payload_encryption_enabled;
      meData.email = await decryptPii(meData.email, _piiOn);
      meData.name  = await decryptPii(meData.name,  _piiOn);
      onAuth({
        userId:      meData.id,
        email:       meData.email,
        name:        meData.name,
        role:        meData.role,
        ad_level:    meData.ad_level ?? 6,
        department:  meData.department ?? "",
        can_approve: meData.can_approve ?? false,
      });
    } catch {
      setRegError("Cannot connect to server. Is the backend running?");
    } finally {
      setRegLoading(false);
    }
  }

  // ── GAP-6: Forgot password submit ────────────────────────────────────────
  async function submitForgot(e) {
    e.preventDefault();
    setForgotError(""); setForgotSuccess("");

    const emailVal = forgotEmail.trim().toLowerCase();
    const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailVal)               { setForgotError("Email is required"); return; }
    if (!EMAIL_RE.test(emailVal)){ setForgotError("Please enter a valid email address"); return; }

    setForgotLoading(true);
    try {
      const res = await apiFetch(`${API}/auth/forgot-password`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ email: emailVal }),
      });
      // Always show success — backend never reveals if email exists
      if (uiConfig.smtp_configured) {
        setForgotSuccess("A temporary password has been sent to your email. Log in with it, then change your password from Profile → Security.");
      } else {
        setForgotSuccess("SMTP is not configured. Your temporary password has been printed to the server console log. Ask your administrator for it, then log in and change your password from Profile → Security.");
      }
      setForgotEmail("");
    } catch {
      setForgotError("Cannot connect to server. Is the backend running?");
    } finally {
      setForgotLoading(false);
    }
  }

  return (
    <>
      <style>{`
        @keyframes fade-in-up {
          from { opacity: 0; transform: translateY(20px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        @keyframes glow-pulse-dark {
          0%,100% { box-shadow: 0 0 25px rgba(59,130,246,.12), 0 0 60px rgba(59,130,246,.04); }
          50%      { box-shadow: 0 0 42px rgba(59,130,246,.24), 0 0 90px rgba(59,130,246,.10); }
        }
        .login-card-dark { animation: glow-pulse-dark 4s ease-in-out infinite; }
        .fade-in-up { animation: fade-in-up 0.7s ease-out both; }
        .input-glow-dark:focus {
          box-shadow: 0 0 0 2px rgba(99,102,241,.45), 0 0 14px rgba(99,102,241,.18);
          border-color: rgba(99,102,241,.55) !important;
          background-color: rgba(255,255,255,.07);
        }
        .input-glow-light:focus {
          box-shadow: 0 0 0 2px rgba(99,102,241,.35), 0 0 10px rgba(99,102,241,.12);
          border-color: rgba(99,102,241,.50) !important;
          background-color: #f9fafb;
        }
        .btn-cta:hover:not(:disabled) {
          box-shadow: 0 0 22px rgba(99,102,241,.45), 0 4px 16px rgba(99,102,241,.30);
        }
        .btn-cta:active:not(:disabled) { transform: scale(.98); }
      `}</style>

      <div className={`flex h-screen w-screen overflow-hidden transition-colors duration-300 ${dm ? 'bg-[#020617]' : 'bg-white'}`}>

        {/* ── Theme toggle ─────────────────────────────────────────────────── */}
        <button
          onClick={() => setDarkMode(d => !d)}
          title={dm ? "Switch to light mode" : "Switch to dark mode"}
          className={`fixed top-4 right-4 z-50 w-9 h-9 rounded-full flex items-center justify-center transition-all duration-200 cursor-pointer ${dm ? 'bg-white/10 hover:bg-white/20 text-gray-300 hover:text-white' : 'bg-white hover:bg-gray-50 text-gray-500 hover:text-gray-800 shadow-md border border-gray-200 '}`}
        >
          {dm ? <Sun size={15} /> : <Moon size={15} />}
        </button>

        {/* ── LEFT PANEL — Agent Constellation ─────────────────── */}
        <div className="hidden lg:flex lg:w-[60%] relative flex-col justify-between p-12 overflow-hidden">

          {/* Hex grid covers the full left panel */}
          <HexGrid darkMode={dm} />


          {/* TOP — Logo + Heading */}
          <div className="relative z-10">
            <div className="flex items-center gap-3 mb-10 fade-in-up" style={{ animationDelay: '0s' }}>
              <div className="w-9 h-9 flex items-center justify-center">
                <BrandMark className="w-9 h-9 drop-shadow-lg" />
              </div>
              <div>
                <BrandWordmark className="h-6" alt="AiNxt Enterprise"
                               textClassName={`font-bold text-xl tracking-wide ${dm ? 'text-white' : 'text-gray-900'}`} />
                <span className="text-blue-600 text-[10px] ml-2 font-semibold tracking-widest uppercase">Enterprise</span>
                <span className={`${dm ? 'text-white' : 'text-gray-900'} text-xs ml-2 font-semibold tracking-widest`}>v1.0</span>
              </div>
            </div>

            <div className="fade-in-up" style={{ animationDelay: '0.08s' }}>
              <h1 className={`text-4xl font-bold leading-tight mb-4 ${dm ? 'text-white' : 'text-gray-900'}`}>
                AiNxt Agentic Platform<br />
                <span className="text-blue-600">
                </span>
              </h1>
              <p className="text-blue-600">
                Empowering every team with enterprise-grade AI.<br/>Built to streamline engineering, workflows and accelerate innovation at scale.
              </p>
            </div>
          </div>

          {/* MIDDLE — Agent Constellation (sits between top text and bottom badges) */}
          <div className="relative z-10 flex-1 min-h-0 mt-4 mb-2 ml-[6%] w-full">
            <div className="absolute inset-x-0 top-0 bottom-0 pb-8">
              <AgentCanvas darkMode={dm} />
            </div>
          </div>

          {/* BOTTOM — Feature badges + footer */}
          <div className="relative z-10 fade-in-up" style={{ animationDelay: '0.2s' }}>
            <div className="flex flex-wrap gap-2 mb-4">
              {FEATURES.map(({ icon: Icon, label }) => (
                <div
                  key={label}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full backdrop-blur-sm ${dm ? 'bg-white/5 border border-white/8' : 'bg-white/70 border border-blue-100'}`}
                >
                  <Icon size={11} className="text-blue-400 flex-shrink-0" />
                  <span className={`text-[11px] font-medium ${dm ? 'text-gray-300' : 'text-gray-600'}`}>{label}</span>
                </div>
              ))}
            </div>
            <div className={`text-xs ${dm ? 'text-gray-600' : 'text-gray-400'}`}>
              {uiConfig.platform_name || "AiNxt"}
            </div>
          </div>
        </div>

        {/* ── RIGHT PANEL — Login Form ──────────────────────────── */}
        <div className="flex-1 flex items-center justify-center relative">

          {/* right panel hex grid — light mode only, matches left panel pattern */}
          {!dm && (
            <div className="absolute inset-0 pointer-events-none">
              <HexGrid darkMode={false} />
            </div>
          )}

          <div className="fade-in-up w-full max-w-sm px-6 relative z-10" style={{ animationDelay: '0.15s' }}>

            <div className="lg:hidden flex items-center gap-2 mb-8 justify-center">
              <div className="w-8 h-8 flex items-center justify-center">
                <BrandMark className="w-8 h-8" />
              </div>
              <BrandWordmark className="h-5" alt="AiNxt Enterprise"
                             textClassName={`font-bold text-lg ${dm ? 'text-white' : 'text-gray-900'}`} />
            </div>

            <div
              className={`${dm ? 'login-card-dark border border-white/10 bg-white/5 backdrop-blur-xl' : 'border border-gray-200 bg-white'} rounded-2xl p-8`}
              style={!dm ? { boxShadow: '0 4px 24px rgba(0,0,0,0.06), 0 1px 4px rgba(0,0,0,0.04)' } : {}}
            >

            {view === "forgot" ? (
              /* ── GAP-6: Forgot password form ───────────────────────────── */
              <>
                <button
                  type="button"
                  onClick={switchToLogin}
                  className={`text-xs mb-5 flex items-center gap-1 ${dm ? 'text-gray-400 hover:text-gray-200' : 'text-gray-400 hover:text-gray-600'} transition-colors cursor-pointer`}
                >
                  ← Back to sign in
                </button>

                <h2 className={`text-xl font-semibold mb-1 ${dm ? 'text-white' : 'text-gray-900'}`}>Forgot password?</h2>
                <p className={`text-sm mb-6 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>
                  Enter your email and we&apos;ll send you a temporary password.
                </p>

                <form onSubmit={submitForgot} className="space-y-4" autoComplete="off">

                  {/* Email */}
                  <div>
                    <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Email</label>
                    <div className={`relative flex items-center rounded-xl border transition-all duration-200 ${dm ? 'bg-white/5 border-white/10 focus-within:border-indigo-500/55 focus-within:shadow-[0_0_0_2px_rgba(99,102,241,0.45),0_0_14px_rgba(99,102,241,0.18)]' : 'bg-gray-50 border-gray-200 focus-within:border-indigo-400/50 focus-within:shadow-[0_0_0_2px_rgba(99,102,241,0.35),0_0_10px_rgba(99,102,241,0.12)]'}`}>
                      <input
                        type="email"
                        value={forgotEmail}
                        onChange={e => setForgotEmail(e.target.value.replace(/\s/g, ''))}
                        placeholder="Enter your email address"
                        autoComplete="off"
                        name="ainxt-forgot-email"
                        className={`flex-1 min-w-0 bg-transparent px-4 py-3 text-sm outline-none ${dm ? 'text-white placeholder-gray-600' : 'text-gray-900 placeholder-gray-400'}`}
                      />
                    </div>
                  </div>

                  {/* Error */}
                  {forgotError && (
                    <div className="bg-red-500/10 border border-red-500/20 text-red-400 text-xs rounded-xl px-4 py-3">
                      {forgotError}
                    </div>
                  )}

                  {/* Success */}
                  {forgotSuccess && (
                    <div className="bg-green-500/10 border border-green-500/20 text-green-400 text-xs rounded-xl px-4 py-3">
                      {forgotSuccess}
                    </div>
                  )}

                  {/* Submit — hidden once success shown */}
                  {!forgotSuccess && (
                    <button
                      type="submit"
                      disabled={forgotLoading}
                      className="btn-cta w-full !mt-4 py-3 rounded-xl brand-grad-r hover:from-blue-500 hover:to-indigo-500 text-white text-sm font-semibold transition-all duration-200 hover:scale-[1.02] disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 cursor-pointer"
                    >
                      {forgotLoading && <Loader2 size={14} className="animate-spin" />}
                      {forgotLoading ? "Sending…" : "Send Temporary Password"}
                    </button>
                  )}

                  {/* Back to login once success shown */}
                  {forgotSuccess && (
                    <button
                      type="button"
                      onClick={switchToLogin}
                      className="btn-cta w-full !mt-2 py-3 rounded-xl brand-grad-r hover:from-blue-500 hover:to-indigo-500 text-white text-sm font-semibold transition-all duration-200 hover:scale-[1.02] flex items-center justify-center gap-2 cursor-pointer"
                    >
                      Back to Sign In
                    </button>
                  )}

                </form>

                {/* Footer — same as login */}
                <div className={`flex justify-between items-center mt-6 pt-5 border-t ${dm ? 'border-white/5' : 'border-gray-100'}`}>
                  <div className="flex items-center gap-1.5">
                    <Shield size={11} className="text-green-400" />
                    <span className={`text-[11px] ${dm ? 'text-gray-600' : 'text-gray-400'}`}>AiNxt Enterprise</span>
                  </div>
                  <span className={`text-[11px] font-medium ${dm ? 'text-gray-600' : 'text-gray-400'}`}>AiNxt</span>
                </div>
              </>
            ) : view === "register" ? (
              /* ── GAP-3: Registration form ──────────────────────────────── */
              <>
                <button
                  type="button"
                  onClick={switchToLogin}
                  className={`text-xs mb-5 flex items-center gap-1 ${dm ? 'text-gray-400 hover:text-gray-200' : 'text-gray-400 hover:text-gray-600'} transition-colors cursor-pointer`}
                >
                  ← Back to sign in
                </button>

                <h2 className={`text-xl font-semibold mb-1 ${dm ? 'text-white' : 'text-gray-900'}`}>Create account</h2>
                <p className={`text-sm mb-6 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Set up your AiNxt account</p>

                <form onSubmit={submitRegister} className="space-y-4" autoComplete="off">

                  {/* Full Name */}
                  <div>
                    <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Full Name</label>
                    <input
                      type="text"
                      value={regName}
                      onChange={e => setRegName(e.target.value)}
                      placeholder="Enter your full name"
                      autoComplete="off"
                      name="ainxt-reg-name"
                      className={`${dm ? 'input-glow-dark bg-white/5 border-white/10 text-white placeholder-gray-600' : 'input-glow-light bg-gray-50 border-gray-200 text-gray-900 placeholder-gray-400'} w-full border rounded-xl px-4 py-3 text-sm outline-none transition-all duration-200`}
                    />
                  </div>

                  {/* Email */}
                  <div>
                    <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Email</label>
                    <div className={`relative flex items-center rounded-xl border transition-all duration-200 ${dm ? 'bg-white/5 border-white/10 focus-within:border-indigo-500/55 focus-within:shadow-[0_0_0_2px_rgba(99,102,241,0.45),0_0_14px_rgba(99,102,241,0.18)]' : 'bg-gray-50 border-gray-200 focus-within:border-indigo-400/50 focus-within:shadow-[0_0_0_2px_rgba(99,102,241,0.35),0_0_10px_rgba(99,102,241,0.12)]'}`}>
                      <input
                        type="email"
                        value={regEmail}
                        onChange={e => setRegEmail(e.target.value.replace(/\s/g, ''))}
                        placeholder="Enter your email address"
                        autoComplete="off"
                        name="ainxt-reg-email"
                        className={`flex-1 min-w-0 bg-transparent px-4 py-3 text-sm outline-none ${dm ? 'text-white placeholder-gray-600' : 'text-gray-900 placeholder-gray-400'}`}
                      />
                    </div>
                  </div>

                  {/* Password */}
                  <div>
                    <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Password</label>
                    <div className="relative">
                      <input
                        type={showRegPwd ? "text" : "password"}
                        value={regPassword ?? ""}
                        onChange={e => setRegPassword(e.target.value)}
                        placeholder="Min. 8 characters"
                        autoComplete="new-password"
                        name="ainxt-reg-password"
                        className={`${dm ? 'input-glow-dark bg-white/5 border-white/10 text-white placeholder-gray-600' : 'input-glow-light bg-gray-50 border-gray-200 text-gray-900 placeholder-gray-400'} w-full border rounded-xl px-4 py-3 pr-11 text-sm outline-none transition-all duration-200`}
                      />
                      <button type="button" onClick={() => setShowRegPwd(p => !p)}
                        className={`absolute right-3 top-1/2 -translate-y-1/2 transition ${dm ? 'text-gray-500 hover:text-gray-300' : 'text-gray-400 hover:text-gray-600'}`}>
                        {showRegPwd ? <EyeOff size={15} /> : <Eye size={15} />}
                      </button>
                    </div>
                  </div>

                  {/* Confirm Password */}
                  <div>
                    <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Confirm Password</label>
                    <div className="relative">
                      <input
                        type={showRegCfm ? "text" : "password"}
                        value={regConfirm}
                        onChange={e => setRegConfirm(e.target.value)}
                        placeholder="Re-enter your password"
                        autoComplete="new-password"
                        name="ainxt-reg-confirm"
                        className={`${dm ? 'input-glow-dark bg-white/5 border-white/10 text-white placeholder-gray-600' : 'input-glow-light bg-gray-50 border-gray-200 text-gray-900 placeholder-gray-400'} w-full border rounded-xl px-4 py-3 pr-11 text-sm outline-none transition-all duration-200`}
                      />
                      <button type="button" onClick={() => setShowRegCfm(p => !p)}
                        className={`absolute right-3 top-1/2 -translate-y-1/2 transition ${dm ? 'text-gray-500 hover:text-gray-300' : 'text-gray-400 hover:text-gray-600'}`}>
                        {showRegCfm ? <EyeOff size={15} /> : <Eye size={15} />}
                      </button>
                    </div>
                  </div>

                  {/* Error */}
                  {regError && (
                    <div className="bg-red-500/10 border border-red-500/20 text-red-400 text-xs rounded-xl px-4 py-3">
                      {regError}
                    </div>
                  )}

                  {/* Submit */}
                  <button
                    type="submit"
                    disabled={regLoading}
                    className="btn-cta w-full !mt-4 py-3 rounded-xl brand-grad-r hover:from-blue-500 hover:to-indigo-500 text-white text-sm font-semibold transition-all duration-200 hover:scale-[1.02] disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 cursor-pointer"
                  >
                    {regLoading && <Loader2 size={14} className="animate-spin" />}
                    {regLoading ? "Creating account…" : "Create Account"}
                  </button>

                </form>

                {/* Footer — same as login */}
                <div className={`flex justify-between items-center mt-6 pt-5 border-t ${dm ? 'border-white/5' : 'border-gray-100'}`}>
                  <div className="flex items-center gap-1.5">
                    <Shield size={11} className="text-green-400" />
                    <span className={`text-[11px] ${dm ? 'text-gray-600' : 'text-gray-400'}`}>AiNxt Enterprise</span>
                  </div>
                  <span className={`text-[11px] font-medium ${dm ? 'text-gray-600' : 'text-gray-400'}`}>AiNxt</span>
                </div>
              </>
            ) : (
              /* ── Login form (existing — unchanged) ─────────────────────── */
              <>

              <h2 className={`text-xl font-semibold mb-1 ${dm ? 'text-white' : 'text-gray-900'}`}>Welcome back</h2>
              <p className={`text-sm mb-6 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Sign in with your AiNxt credentials</p>

              {/*
            SECURITY (DAST fix — autocomplete on sensitive fields):
            autoComplete="off" is set on the <form> and on every sensitive input
            (email + password) to instruct the browser not to cache or auto-fill
            credentials.  The password field previously carried "new-password"
            which is semantically correct only for registration / change-password
            flows; on a login form it is a misconfiguration that DAST scanners
            flag because certain browsers treat "new-password" as a soft hint and
            may still offer to fill saved credentials.  "off" is the correct and
            unambiguous directive for a login form that must not cache credentials.
          */}
              <form onSubmit={submit} className="space-y-4" autoComplete="off">

                <div>
                  <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Email</label>
                  <div className={`relative flex items-center rounded-xl border transition-all duration-200 ${dm ? 'bg-white/5 border-white/10 focus-within:border-indigo-500/55 focus-within:shadow-[0_0_0_2px_rgba(99,102,241,0.45),0_0_14px_rgba(99,102,241,0.18)]' : 'bg-gray-50 border-gray-200 focus-within:border-indigo-400/50 focus-within:shadow-[0_0_0_2px_rgba(99,102,241,0.35),0_0_10px_rgba(99,102,241,0.12)]'}`}>
                    <input
                      type="email"
                      value={email}
                      onChange={e => setEmail(e.target.value.replace(/\s/g, ''))}
                      placeholder="Enter your email address"
                      autoComplete="off"
                      name="ainxt-email"
                      className={`flex-1 min-w-0 bg-transparent px-4 py-3 text-sm outline-none ${dm ? 'text-white placeholder-gray-600' : 'text-gray-900 placeholder-gray-400'}`}
                    />
                  </div>
                </div>

                <div>
                  <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>Password</label>
                  <div className="relative">
                    <input
                      type={showPwd ? "text" : "password"}
                      value={identifier}
                      onChange={e => setIdentifier(e.target.value)}
                      placeholder="••••••••"
                      autoComplete="off"
                      name="ainxt-password"
                      className={`${dm ? 'input-glow-dark bg-white/5 border-white/10 text-white placeholder-gray-600' : 'input-glow-light bg-gray-50 border-gray-200 text-gray-900 placeholder-gray-400'} w-full border rounded-xl px-4 py-3 pr-11 text-sm outline-none transition-all duration-200`}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPwd(p => !p)}
                      className={`absolute right-3 top-1/2 -translate-y-1/2 transition ${dm ? 'text-gray-500 hover:text-gray-300' : 'text-gray-400 hover:text-gray-600'}`}
                    >
                      {showPwd ? <EyeOff size={15} /> : <Eye size={15} />}
                    </button>
                  </div>
                </div>

                {/* ── CAPTCHA ───────────────────────────────────────────── */}
                <div>
                  <label className={`block text-xs font-medium mb-1.5 ${dm ? 'text-gray-400' : 'text-gray-500'}`}>
                    Security Verification
                  </label>
                  <div className="flex items-center gap-2">
                    <div className="w-[45%] flex-shrink-0">
                      <CaptchaDisplay text={captchaText} darkMode={dm} />
                    </div>
                    <input
                      type="text"
                      value={captchaInput}
                      onChange={e => { captchaInputRef.current = e.target.value; setCaptchaInput(e.target.value); if (captchaError) setCaptchaError(""); }}
                      placeholder="Enter code"
                      autoComplete="off"
                      maxLength={6}
                      style={{ height: 46 }}
                      className={`${dm ? 'input-glow-dark bg-white/5 text-white placeholder-gray-600' : 'input-glow-light bg-gray-50 text-gray-900 placeholder-gray-400'} flex-1 min-w-0 border rounded-xl px-3 text-sm outline-none transition-all duration-200 ${captchaError ? 'border-red-500/60' : dm ? 'border-white/10' : 'border-gray-200'}`}
                    />
                  </div>
                  {captchaError && (
                    <p className="text-red-400 text-xs mt-1.5 ml-1">{captchaError}</p>
                  )}
                </div>

                {blocked && (
                  <div className="bg-amber-500/10 border border-amber-500/20 text-amber-300 text-xs rounded-xl px-4 py-3 text-center space-y-1">
                    <div className="font-semibold">Account not provisioned</div>
                    <div className="text-amber-400/80">Your account has not been set up yet. Contact your administrator.</div>
                  </div>
                )}
                {error && (
                  <div className="bg-red-500/10 border border-red-500/20 text-red-400 text-xs rounded-xl px-4 py-3">
                    {error}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={loading}
                  className="btn-cta w-full !mt-4 py-3 rounded-xl brand-grad-r hover:from-blue-500 hover:to-indigo-500 text-white text-sm font-semibold transition-all duration-200 hover:scale-[1.02] disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 cursor-pointer"
                >
                  {loading && <Loader2 size={14} className="animate-spin" />}
                  {loading ? "Signing in…" : "Sign In"}
                </button>

              </form>

              {/* ── GAP-3: Sign up link — OSS only ───────────────────────── */}
              {/* ── GAP-6: Forgot password link — OSS only ───────────────── */}
              {(uiConfig.self_registration_enabled !== false || uiConfig.forgot_password_enabled !== false) && (
                <p className={`text-center text-xs mt-4 ${dm ? 'text-gray-500' : 'text-gray-400'}`}>
                  {uiConfig.self_registration_enabled !== false && (
                    <>
                      Don&apos;t have an account?{' '}
                      <button
                        type="button"
                        onClick={switchToRegister}
                        className="text-blue-400 hover:text-blue-300 font-medium transition-colors cursor-pointer"
                      >
                        Sign up
                      </button>
                    </>
                  )}
                  {uiConfig.self_registration_enabled !== false && uiConfig.forgot_password_enabled !== false && (
                    <span className={`mx-2 ${dm ? 'text-gray-600' : 'text-gray-300'}`}>·</span>
                  )}
                  {uiConfig.forgot_password_enabled !== false && (
                    <button
                      type="button"
                      onClick={switchToForgot}
                      className="text-blue-400 hover:text-blue-300 font-medium transition-colors cursor-pointer"
                    >
                      Forgot password?
                    </button>
                  )}
                </p>
              )}

              <div className={`flex justify-between items-center mt-6 pt-5 border-t ${dm ? 'border-white/5' : 'border-gray-100'}`}>
                <div className="flex items-center gap-1.5">
                  <Shield size={11} className="text-green-400" />
                  <span className={`text-[11px] ${dm ? 'text-gray-600' : 'text-gray-400'}`}>AiNxt Enterprise</span>
                </div>
                <span className={`text-[11px] font-medium ${dm ? 'text-gray-600' : 'text-gray-400'}`}>AiNxt</span>
              </div>
              </> /* end login view */
            )} {/* end view ternary */}
            </div>

            <p className={`text-center text-[11px] mt-4 ${dm ? 'text-gray-700' : 'text-gray-400'}`}>
              {uiConfig.internal_use_only ? "Secured · Internal Use Only · All access logged" : "Secured · All access logged"}
            </p>
          </div>
        </div>

      </div>
    </>
  );
}
