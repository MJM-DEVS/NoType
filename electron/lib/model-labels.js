// Model names for tray and toast texts. No electron import: unit-tested with
// `node --test` against model-catalog.js, which the settings pages use.
const MODEL_LABELS = {
  'tiny': 'Whisper Tiny', 'base': 'Whisper Base', 'small': 'Whisper Small',
  'medium': 'Whisper Medium', 'large-v2': 'Whisper Large v2', 'large-v3': 'Whisper Large v3',
  'large-v3-turbo': 'Whisper Large v3 Turbo', 'distil-large-v3': 'Distil Large v3',
  'distil-large-v3.5': 'Distil Large v3.5', 'german-turbo': 'Large v3 Turbo German',
};

function modelLabel(id) { return MODEL_LABELS[id] || id; }

module.exports = { MODEL_LABELS, modelLabel };
