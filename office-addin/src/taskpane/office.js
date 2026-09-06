// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
/* Helpers to extract context from the current Office item. */

export function getHostType() {
  try { return Office.context.host; } catch { return null; }
}

export async function getEmailContext() {
  return new Promise((resolve) => {
    const item = Office.context.mailbox?.item;
    if (!item) return resolve(null);

    // The signed-in user (so a draft signs off with the user's real name, never
    // "[Your Name]", and we never greet the user as if they were the sender).
    let userName = "", userEmail = "";
    try {
      userName  = Office.context.mailbox?.userProfile?.displayName || "";
      userEmail = Office.context.mailbox?.userProfile?.emailAddress || "";
    } catch { /* */ }

    // Read mode → item.subject is a STRING. Compose/reply → it's a Subject object
    // with getAsync (using it directly renders "[object Object]").
    const isReadMode = typeof item.subject === "string";

    const ctx = {
      subject: isReadMode ? item.subject : "",
      from: "", fromName: "",
      to: [], toNames: [],
      body: "",
      userName, userEmail,
      itemType: item.itemType,
      isReadMode,
    };

    const tasks = [];

    // Subject (compose mode: async)
    if (!isReadMode && item.subject?.getAsync) {
      tasks.push(new Promise(res => item.subject.getAsync(r => {
        ctx.subject = r.status === Office.AsyncResultStatus.Succeeded ? (r.value || "") : "";
        res();
      })));
    }

    // Sender (read mode) — name + email
    if (item.from?.getAsync) {
      tasks.push(new Promise(res => item.from.getAsync(r => {
        ctx.from = r.value?.emailAddress || ""; ctx.fromName = r.value?.displayName || ""; res();
      })));
    } else if (item.from?.emailAddress) {
      ctx.from = item.from.emailAddress; ctx.fromName = item.from.displayName || "";
    }

    // Recipients — name + email (the reply target in compose mode)
    if (item.to?.getAsync) {
      tasks.push(new Promise(res => item.to.getAsync(r => {
        const arr = r.value || [];
        ctx.to = arr.map(x => x.emailAddress); ctx.toNames = arr.map(x => x.displayName || x.emailAddress);
        res();
      })));
    } else if (Array.isArray(item.to)) {
      ctx.to = item.to.map(x => x.emailAddress); ctx.toNames = item.to.map(x => x.displayName || x.emailAddress);
    }

    // Body (plain text — usually contains the quoted prior thread inline)
    if (item.body?.getAsync) {
      tasks.push(new Promise(res => item.body.getAsync(
        Office.CoercionType.Text,
        r => { ctx.body = (r.value || "").slice(0, 6000); res(); }
      )));
    }

    Promise.all(tasks).then(() => resolve(ctx));
  });
}

export async function insertTextToCompose(text) {
  return new Promise((resolve, reject) => {
    const item = Office.context.mailbox?.item;
    if (!item?.body?.setAsync) return reject(new Error("Not in compose mode"));
    item.body.setAsync(text, { coercionType: Office.CoercionType.Html }, r => {
      r.status === Office.AsyncResultStatus.Succeeded ? resolve() : reject(r.error);
    });
  });
}

export async function prependToCompose(text) {
  return new Promise((resolve, reject) => {
    const item = Office.context.mailbox?.item;
    if (!item?.body?.prependAsync) return reject(new Error("Not in compose mode"));
    item.body.prependAsync(
      `<p style="background:#f0f4ff;padding:8px;border-left:3px solid #3b82f6;">${text}</p>`,
      { coercionType: Office.CoercionType.Html },
      r => r.status === Office.AsyncResultStatus.Succeeded ? resolve() : reject(r.error)
    );
  });
}
