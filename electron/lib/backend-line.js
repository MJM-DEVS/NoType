// One line of the backend's stdout → { type, data } or the reason it isn't
// a message. No electron import: unit-tested with `node --test`.
//
// Lines can carry dictated text (transcription_done, preview, agent_sending),
// so nothing here ever returns or logs the line itself, only its length.
function parseBackendLine(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (_) {
    return { error: `not JSON (${String(line).length} chars)` };
  }
  if (!msg || typeof msg !== 'object' || Array.isArray(msg) || typeof msg.type !== 'string') {
    return { error: `no message type (${String(line).length} chars)` };
  }
  return { type: msg.type, data: msg.data };
}

module.exports = { parseBackendLine };
