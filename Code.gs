/**
 * Auto Email Sender — CLOUD SIDE (Google Apps Script)
 * ----------------------------------------------------
 * Receives a batch of emails via POST, immediately creates Gmail DRAFTS
 * (optionally with attachments), then sends them at the requested time via
 * time-based triggers — entirely on Google's servers. Your laptop can be off.
 * Does NOT use Gmail's "Schedule send", so the 100-scheduled-sends cap does
 * not apply.
 *
 * Deploy (one-time, ~5 min — see README.md):
 *   account    : the Google account you want to send from
 *   services   : add "Gmail API" under Services (Advanced Gmail Service)
 *   properties : Project Settings -> Script properties -> add SECRET = <long random string>
 *                optional: SENDER_NAME = display name for outgoing mail
 *                optional: DEFAULT_ATTACHMENT_FILE_ID = Drive file id attached to
 *                          every email unless a batch opts out (e.g. a PDF you
 *                          always send). Leave unset for no default attachment.
 *   deploy     : New deployment -> Web app -> Execute as: Me -> Anyone with the link
 *   then       : run installDailySweep() once from the editor (REQUIRED safety net)
 *
 * Design notes (hardened after adversarial review):
 *  - submit is IDEMPOTENT via client_key: retrying a timed-out POST can never
 *    draft or send the same chunk twice.
 *  - sendDueBatches arms a RESCUE trigger before doing anything, quarantines
 *    corrupt batch records instead of dying on them, persists state after
 *    EVERY send, and treats "draft not found" as terminal (sent_assumed).
 *  - The dailySweep trigger is the load-bearing backstop — install it.
 *
 * v4: arbitrary attachments — submit accepts req.attachments =
 *   [{filename, mimeType, data(base64)}] applied to EVERY email in the chunk
 *   (on top of / instead of the default attachment). Attachments live only in
 *   the created drafts; nothing is persisted to Script Properties. ping
 *   reports version.
 */

// ====== CONFIG ======
var VERSION = 4;
var MAX_ATTACH_COUNT = 10;        // arbitrary attachments per chunk
var MAX_ATTACH_BYTES = 22 * 1024 * 1024;  // decoded total; Gmail caps messages at 25MB
var MAX_SEND_ATTEMPTS = 3;        // per email (transient errors only)
var RETRY_DELAY_MIN = 15;         // minutes between retry sweeps
var CHUNK_MAX = 30;               // refuse bigger single POSTs (6-min execution guard)
var DONE_RETENTION_DAYS = 7;      // purge finished batch records after this
var ERR_TRUNC = 150;              // stored error-string length cap
var EMAIL_RX = /^[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]{2,}$/;

function secret_() {
  return PropertiesService.getScriptProperties().getProperty('SECRET') || '';
}
function defaultAttachmentId_() {
  return PropertiesService.getScriptProperties().getProperty('DEFAULT_ATTACHMENT_FILE_ID') || '';
}
function senderName_() {
  return PropertiesService.getScriptProperties().getProperty('SENDER_NAME') || '';
}

// ====== HTTP entrypoints ======

function doGet() {
  return jsonOut({ok: true, service: 'auto-email-sender', hint: 'POST JSON with {secret, action}'});
}

function doPost(e) {
  var req;
  try {
    req = JSON.parse(e && e.postData && e.postData.contents || '');
  } catch (err) {
    return jsonOut({ok: false, error: 'bad JSON body'});
  }
  var sec = secret_();
  if (!sec) return jsonOut({ok: false, error: 'SECRET not configured (Script properties)'});
  if (!req || typeof req.secret !== 'string' || req.secret !== sec) {
    return jsonOut({ok: false, error: 'auth failed'});
  }
  try {
    switch (req.action) {
      case 'ping':     return jsonOut({ok: true, pong: true, version: VERSION, account: ownerEmail_(), now: new Date().toISOString()});
      case 'submit':   return jsonOut(submitBatch_(req));
      case 'status':   return jsonOut(statusReport_(req));
      case 'cancel':   return jsonOut(cancelBatch_(req));
      case 'send_now': return jsonOut(sendNow_(req));
      default:         return jsonOut({ok: false, error: 'unknown action: ' + req.action});
    }
  } catch (err) {
    return jsonOut({ok: false, error: String(err).slice(0, 300)});
  }
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function ownerEmail_() {
  try { return Session.getEffectiveUser().getEmail(); } catch (e) { return ''; }
}

// ====== actions ======

/**
 * submit: {secret, action:'submit', send_at_ms, client_key, label?, attach_default?,
 *          attachments?: [{filename, mimeType, data(base64)}, ...],   // applied to EVERY email
 *          emails: [{to, subject, body, html?}, ...]}   (≤ CHUNK_MAX per POST; CLI chunks)
 * Validates EVERYTHING first (no partial drafting), is idempotent on client_key,
 * creates drafts NOW, schedules the send for send_at_ms.
 */
function submitBatch_(req) {
  var emails = req.emails || [];
  if (!emails.length) return {ok: false, error: 'no emails'};
  if (emails.length > CHUNK_MAX) return {ok: false, error: 'chunk too big (max ' + CHUNK_MAX + ' per POST)'};
  var sendAtMs = Number(req.send_at_ms);
  if (!sendAtMs || isNaN(sendAtMs)) return {ok: false, error: 'send_at_ms missing/invalid'};
  if (sendAtMs < Date.now() - 60 * 1000) return {ok: false, error: 'send_at_ms is in the past'};
  if (sendAtMs > Date.now() + 35 * 24 * 3600 * 1000) return {ok: false, error: 'send_at_ms more than 35 days out'};
  var clientKey = String(req.client_key || '');
  if (!clientKey || clientKey.length < 8) return {ok: false, error: 'client_key (>=8 chars) required for idempotency'};

  // ---- validate ALL emails server-side before creating anything ----
  for (var v = 0; v < emails.length; v++) {
    var em0 = emails[v];
    if (!em0 || typeof em0.to !== 'string' || !EMAIL_RX.test(em0.to.trim())) {
      return {ok: false, error: 'invalid to-address at index ' + v + ': ' + String(em0 && em0.to).slice(0, 80)};
    }
    if (typeof em0.subject !== 'string' || !em0.subject.trim() || em0.subject.length > 200 ||
        /[\r\n]/.test(em0.subject)) {
      return {ok: false, error: 'invalid subject at index ' + v};
    }
  }

  // ---- decode + validate arbitrary attachments (before any drafting) ----
  var extraBlobs = [];
  if (req.attachments && req.attachments.length) {
    if (req.attachments.length > MAX_ATTACH_COUNT) {
      return {ok: false, error: 'too many attachments (max ' + MAX_ATTACH_COUNT + ')'};
    }
    var totalBytes = 0;
    for (var ai = 0; ai < req.attachments.length; ai++) {
      var at = req.attachments[ai];
      if (!at || typeof at.data !== 'string' || !at.data) {
        return {ok: false, error: 'attachment ' + ai + ' missing base64 data'};
      }
      var bytes;
      try { bytes = Utilities.base64Decode(at.data); }
      catch (decErr) { return {ok: false, error: 'attachment ' + ai + ' is not valid base64'}; }
      totalBytes += bytes.length;
      if (totalBytes > MAX_ATTACH_BYTES) {
        return {ok: false, error: 'attachments exceed ' + Math.round(MAX_ATTACH_BYTES / 1048576) + 'MB decoded total'};
      }
      extraBlobs.push(Utilities.newBlob(bytes,
        String(at.mimeType || 'application/octet-stream'),
        String(at.filename || ('attachment-' + (ai + 1))).slice(0, 120)));
    }
  }

  // ---- idempotency claim (lock) ----
  var props = PropertiesService.getScriptProperties();
  var keyProp = 'key_' + clientKey;
  var claim = null;
  withLock_(function () {
    var existing = props.getProperty(keyProp);
    if (existing) { claim = JSON.parse(existing); return; }
    props.setProperty(keyProp, JSON.stringify({claimedAt: Date.now()}));
  });
  if (claim) {
    if (claim.batch_id) {                       // finished before — return stored receipt
      return {ok: true, deduped: true, batch_id: claim.batch_id, drafted: claim.drafted,
              draft_failures: [], send_at_utc: new Date(claim.sendAtMs || sendAtMs).toISOString()};
    }
    if (Date.now() - (claim.claimedAt || 0) < 10 * 60 * 1000) {
      return {ok: false, error: 'submit with this client_key is already in flight — wait, then check status'};
    }
    // stale claim (>10 min, no batch record): previous attempt died mid-draft.
    // Orphan drafts may exist in the Drafts folder (visible, never auto-sent). Proceed fresh.
    withLock_(function () { props.setProperty(keyProp, JSON.stringify({claimedAt: Date.now()})); });
  }

  // ---- create drafts (slow part — outside the lock) ----
  var attachBlob = null;
  if (req.attach_default !== false) {
    var defaultId = defaultAttachmentId_();
    if (defaultId) attachBlob = DriveApp.getFileById(defaultId).getBlob();
  }
  var batchId = 'b' + Utilities.formatDate(new Date(sendAtMs), 'UTC', 'MMddHHmm') + '_' +
                Math.random().toString(36).slice(2, 7);
  var items = [], failures = [];
  for (var i = 0; i < emails.length; i++) {
    var em = emails[i];
    try {
      var opts = {};
      var senderName = senderName_();
      if (senderName) opts.name = senderName;
      var atts = (attachBlob ? [attachBlob] : []).concat(extraBlobs);
      if (atts.length) opts.attachments = atts;
      if (em.html) opts.htmlBody = em.html;
      var d = GmailApp.createDraft(em.to.trim(), em.subject, String(em.body || ''), opts);
      items.push({to: em.to.trim(), draftId: d.getId(), state: 'pending', attempts: 0});
    } catch (err) {
      failures.push({to: em.to, error: String(err).slice(0, ERR_TRUNC)});
    }
  }

  // ---- persist + map key (lock); clean up drafts if persisting fails ----
  try {
    withLock_(function () {
      if (items.length) {
        props.setProperty('batch_' + batchId, JSON.stringify({
          id: batchId, sendAtMs: sendAtMs, created: Date.now(),
          label: String(req.label || '').slice(0, 80), state: 'pending', items: items
        }));
      }
      props.setProperty(keyProp, JSON.stringify({
        batch_id: batchId, drafted: items.length, sendAtMs: sendAtMs, claimedAt: Date.now()
      }));
    });
  } catch (persistErr) {
    for (var c = 0; c < items.length; c++) {
      try { Gmail.Users.Drafts.remove('me', items[c].draftId); } catch (ignored) {}
    }
    try { withLock_(function () { props.deleteProperty(keyProp); }); } catch (ignored2) {}
    return {ok: false, error: 'persist failed, drafts cleaned up — safe to retry: ' + String(persistErr).slice(0, 150)};
  }

  // ---- arm trigger; a trigger failure must NOT look like a failed submit ----
  var warning = null;
  if (items.length) {
    try { armTrigger_(sendAtMs); }
    catch (trigErr) { warning = 'trigger creation failed (' + String(trigErr).slice(0, 100) +
                                ') — dailySweep will deliver this batch'; }
  }

  var res = {ok: true, batch_id: batchId, drafted: items.length, draft_failures: failures,
             send_at_utc: new Date(sendAtMs).toISOString(), attached: extraBlobs.length};
  if (warning) res.warning = warning;
  return res;
}

/** status: {action:'status', batch_id?, verbose?} */
function statusReport_(req) {
  var props = PropertiesService.getScriptProperties().getProperties();
  var out = [], corrupt = [];
  for (var k in props) {
    if (k.indexOf('batch_') !== 0) continue;
    var b;
    try { b = JSON.parse(props[k]); } catch (err) { corrupt.push(k); continue; }
    if (req.batch_id && b.id !== req.batch_id) continue;
    var entry = {batch_id: b.id, label: b.label, state: b.state,
                 send_at_utc: new Date(b.sendAtMs).toISOString(), counts: countStates_(b.items)};
    if (req.verbose) entry.items = b.items;
    out.push(entry);
  }
  out.sort(function (a, b) { return a.send_at_utc < b.send_at_utc ? -1 : 1; });
  var res = {ok: true, account: ownerEmail_(), batches: out};
  if (corrupt.length) res.corrupt_records = corrupt;
  return res;
}

/** cancel: {action:'cancel', batch_id, trash_drafts?} — only pending items are affected */
function cancelBatch_(req) {
  if (!req.batch_id) return {ok: false, error: 'batch_id required'};
  var res = {ok: true, batch_id: req.batch_id, cancelled: 0, trashed: 0};
  withLock_(function () {
    var props = PropertiesService.getScriptProperties();
    var raw = props.getProperty('batch_' + req.batch_id);
    if (!raw) { res = {ok: false, error: 'no such batch'}; return; }
    var b = JSON.parse(raw);
    for (var i = 0; i < b.items.length; i++) {
      var it = b.items[i];
      if (it.state !== 'pending') continue;
      it.state = 'cancelled';
      res.cancelled++;
      if (req.trash_drafts) {
        try { Gmail.Users.Drafts.remove('me', it.draftId); res.trashed++; }
        catch (err) { it.lastError = ('trash failed: ' + String(err)).slice(0, ERR_TRUNC); }
      }
    }
    if (res.cancelled > 0) {
      b.state = 'cancelled';
      b.finishedAt = Date.now();
    } else {
      res.note = 'nothing was pending (already sent / failed / cancelled) — state unchanged';
    }
    props.setProperty('batch_' + b.id, JSON.stringify(b));
  });
  return res;
}

/** send_now: {action:'send_now', batch_id} — fire a batch immediately */
function sendNow_(req) {
  if (!req.batch_id) return {ok: false, error: 'batch_id required'};
  var result = null;
  withLock_(function () {
    var props = PropertiesService.getScriptProperties();
    var raw = props.getProperty('batch_' + req.batch_id);
    if (!raw) { result = {ok: false, error: 'no such batch'}; return; }
    var b = JSON.parse(raw);
    if (b.state === 'cancelled') { result = {ok: false, error: 'batch is cancelled'}; return; }
    processBatch_(b, props);
    result = {ok: true, batch_id: b.id, state: b.state, counts: countStates_(b.items)};
  });
  return result;
}

// ====== the scheduled sender ======

/**
 * Trigger handler. Hardened:
 *  - on lock contention: re-arms itself +5 min and exits (the spent trigger is replaced)
 *  - arms a RESCUE trigger (+10 min) BEFORE deleting anything; deletes it only on success
 *  - quarantines corrupt batch records instead of dying on them
 *  - sends every due batch (catch-up included), then re-arms future/retry triggers
 */
function sendDueBatches() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30 * 1000)) {
    ScriptApp.newTrigger('sendDueBatches').timeBased().at(new Date(Date.now() + 5 * 60 * 1000)).create();
    return;
  }
  try {
    var rescue = ScriptApp.newTrigger('sendDueBatches').timeBased()
                  .at(new Date(Date.now() + 10 * 60 * 1000)).create();
    var rescueId = rescue.getUniqueId();

    var triggers = ScriptApp.getProjectTriggers();
    for (var i = 0; i < triggers.length; i++) {
      if (triggers[i].getHandlerFunction() === 'sendDueBatches' &&
          triggers[i].getUniqueId() !== rescueId) {
        ScriptApp.deleteTrigger(triggers[i]);
      }
    }

    var props = PropertiesService.getScriptProperties();
    var all = props.getProperties();
    var now = Date.now();
    var rearm = [];
    for (var k in all) {
      // purge expired idempotency claims — without this, key_* records accumulate
      // forever and eventually exhaust the 500KB Script Properties quota. A claim
      // must outlive both its submit-retry window and the batch's send time, so
      // age is measured from the LATER of claimedAt / sendAtMs.
      if (k.indexOf('key_') === 0) {
        try {
          var claim = JSON.parse(all[k]);
          var anchor = Math.max(Number(claim.claimedAt) || 0, Number(claim.sendAtMs) || 0);
          if (now - anchor > DONE_RETENTION_DAYS * 24 * 3600 * 1000) props.deleteProperty(k);
        } catch (badClaim) {
          try { props.deleteProperty(k); } catch (ignored) {}
        }
        continue;
      }
      if (k.indexOf('batch_') !== 0) continue;
      try {
        var b = JSON.parse(all[k]);
        var terminal = (b.state === 'done' || b.state === 'cancelled' || b.state === 'done_with_failures');
        if (terminal) {
          // retention counts from when the batch FINISHED (or was due to send), not
          // from creation — else a batch scheduled weeks out is purged the day it sends
          var doneAnchor = Math.max(Number(b.finishedAt) || 0, Number(b.sendAtMs) || 0, Number(b.created) || 0);
          if (now - doneAnchor > DONE_RETENTION_DAYS * 24 * 3600 * 1000) props.deleteProperty(k);
          continue;
        }
        if (b.sendAtMs > now + 60 * 1000) { rearm.push(b.sendAtMs); continue; }
        processBatch_(b, props);
        if (b.state === 'pending') rearm.push(Date.now() + RETRY_DELAY_MIN * 60 * 1000);
      } catch (perBatchErr) {
        // quarantine — one bad record must never strand the pipeline
        try {
          props.setProperty('bad_' + k + '_' + Date.now(), String(all[k]).slice(0, 8000));
          props.deleteProperty(k);
        } catch (ignored) {}
      }
    }

    // de-dup re-arm times into 2-min buckets, keep the earliest 15
    // (each firing re-arms from full state, so dropped later buckets self-recover)
    var buckets = {};
    for (var j = 0; j < rearm.length; j++) {
      var key = Math.round(rearm[j] / (2 * 60 * 1000));
      if (!buckets[key] || rearm[j] < buckets[key]) buckets[key] = rearm[j];
    }
    var times = Object.keys(buckets).map(function (x) { return buckets[x]; })
                  .sort(function (a, b) { return a - b; }).slice(0, 15);
    for (var t = 0; t < times.length; t++) {
      ScriptApp.newTrigger('sendDueBatches').timeBased().at(new Date(times[t])).create();
    }

    // success — remove the rescue trigger
    var after = ScriptApp.getProjectTriggers();
    for (var r = 0; r < after.length; r++) {
      if (after[r].getUniqueId() === rescueId) ScriptApp.deleteTrigger(after[r]);
    }
  } finally {
    lock.releaseLock();
  }
}

function processBatch_(b, props) {
  var key = 'batch_' + b.id;
  for (var i = 0; i < b.items.length; i++) {
    var item = b.items[i];
    if (item.state !== 'pending') continue;
    try {
      Gmail.Users.Drafts.send({id: item.draftId}, 'me');
      item.state = 'sent';
      item.sentAt = Date.now();
    } catch (err) {
      var msg = String(err);
      if (/not\s*found|404/i.test(msg)) {
        // draft consumed (sent manually / by a crashed earlier run) or deleted — terminal
        item.state = 'sent_assumed';
        item.lastError = 'draft gone — verify in Sent before any re-send'.slice(0, ERR_TRUNC);
      } else {
        item.attempts = (item.attempts || 0) + 1;
        item.lastError = msg.slice(0, ERR_TRUNC);
        if (item.attempts >= MAX_SEND_ATTEMPTS) item.state = 'failed';
      }
    }
    // persist after EVERY item so a mid-run kill never loses sent-markers
    try { props.setProperty(key, JSON.stringify(b)); } catch (ignored) {}
  }
  var counts = countStates_(b.items);
  if (b.state !== 'cancelled') {
    b.state = counts.pending ? 'pending' : (counts.failed ? 'done_with_failures' : 'done');
  }
  if (b.state !== 'pending' && !b.finishedAt) b.finishedAt = Date.now();
  if (b.state === 'done' || b.state === 'done_with_failures') {
    // slim terminal records (ScriptProperties 9KB/value + 500KB total quotas)
    b.items = b.items.map(function (it) {
      return {to: it.to, state: it.state, lastError: it.lastError ? it.lastError.slice(0, 60) : undefined};
    });
  }
  props.setProperty(key, JSON.stringify(b));
}

// ====== helpers ======

function countStates_(items) {
  var counts = {pending: 0, sent: 0, failed: 0, sent_assumed: 0, cancelled: 0};
  for (var i = 0; i < (items || []).length; i++) {
    var s = items[i].state;
    counts[s] = (counts[s] || 0) + 1;
  }
  return counts;
}

function armTrigger_(ms) {
  // duplicates are harmless (handler deletes all on entry); just cap the count
  var n = 0;
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'sendDueBatches') n++;
  }
  if (n >= 15) return; // plenty already armed; chain + dailySweep cover this batch
  ScriptApp.newTrigger('sendDueBatches').timeBased().at(new Date(Number(ms))).create();
}

function withLock_(fn) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30 * 1000);
  try { fn(); } finally { lock.releaseLock(); }
}

/**
 * REQUIRED setup step — run this ONCE manually from the editor.
 * Daily ~7 AM sweep = the load-bearing backstop that delivers any batch whose
 * one-shot trigger was lost (quota, crash, race). Separate handler name so
 * sendDueBatches' trigger cleanup never deletes it.
 */
function dailySweep() { sendDueBatches(); }
function installDailySweep() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'dailySweep') ScriptApp.deleteTrigger(triggers[i]);
  }
  ScriptApp.newTrigger('dailySweep').timeBased().everyDays(1).atHour(7).create();
}
