// Digest selection handling (issue #18): button custom IDs, text fallback,
// one-selection-per-category store semantics, visual component state, and
// render-queue handoff.
//
// Concurrency contract (review finding P1, PR #42): the bot process is the
// only selection writer, and every selection (status flip + queue enqueue)
// runs inside a per-category async lock, so two simultaneous selections in
// the same category serialize — the second one sees the first one's store
// state and reverts it instead of double-selecting.

export const SELECT_CUSTOM_ID_PREFIX = 'clipsmith:select';

const COMPONENT_TYPE_ACTION_ROW = 1;
const COMPONENT_TYPE_BUTTON = 2;
const BUTTON_STYLE_PRIMARY = 1;
const BUTTON_STYLE_SECONDARY = 2;
const BUTTON_STYLE_SUCCESS = 3;

export function selectionCustomId(category, candidateId, index) {
  return [
    SELECT_CUSTOM_ID_PREFIX,
    encodeURIComponent(category),
    String(index),
    candidateId,
  ].join(':');
}

export function parseSelectionCustomId(customId) {
  if (typeof customId !== 'string') return null;
  const [prefixA, prefixB, encodedCategory, index, candidateId] = customId.split(':');
  if (`${prefixA}:${prefixB}` !== SELECT_CUSTOM_ID_PREFIX || !candidateId) {
    return null;
  }
  const parsedIndex = Number(index);
  if (!Number.isInteger(parsedIndex) || parsedIndex < 1) return null;
  return {
    category: decodeURIComponent(encodedCategory),
    index: parsedIndex,
    candidateId,
  };
}

export function selectionButton({ category, candidateId, index }) {
  return {
    type: COMPONENT_TYPE_BUTTON,
    style: BUTTON_STYLE_PRIMARY,
    custom_id: selectionCustomId(category, candidateId, index),
    label: `Select as ${category} OF THE WEEK`,
  };
}

// discord.js hands back component INSTANCES (ActionRow/ButtonComponent) whose
// fields live under `.data`, not plain API JSON. Spreading an instance loses
// the top-level `type` and produces `{data: {...}}` objects the API rejects
// (review finding P1, PR #42). Normalize everything to API JSON first:
// prefer `toJSON()`, fall back to `.data`, and force a top-level `type`.
function componentToApiJson(component, fallbackType) {
  if (component == null) return component;
  let json = component;
  if (typeof component.toJSON === 'function') json = component.toJSON();
  else if (component.data && typeof component.data === 'object') {
    json = { ...component.data };
  }
  if (json.type == null) json = { ...json, type: fallbackType };
  return json;
}

function normalizeRow(row) {
  const json = componentToApiJson(row, COMPONENT_TYPE_ACTION_ROW);
  const children = json.components ?? row?.components ?? [];
  return {
    ...json,
    type: json.type ?? COMPONENT_TYPE_ACTION_ROW,
    components: Array.from(children, (button) => {
      const buttonJson = componentToApiJson(button, COMPONENT_TYPE_BUTTON);
      const customId = buttonCustomId(buttonJson) ?? buttonCustomId(button);
      return customId == null ? buttonJson : { ...buttonJson, custom_id: customId };
    }),
  };
}

function buttonCustomId(button) {
  return button?.custom_id ?? button?.customId ?? button?.data?.custom_id ?? null;
}

export function selectionsFromMessage(message) {
  const selections = [];
  for (const row of message?.components ?? []) {
    for (const button of normalizeRow(row).components ?? []) {
      const parsed = parseSelectionCustomId(buttonCustomId(button));
      if (parsed) selections.push(parsed);
    }
  }
  return selections.sort((a, b) => a.index - b.index);
}

export function markSelectedComponents(components, selectedCandidateId) {
  return Array.from(components ?? [], (rawRow) => {
    const row = normalizeRow(rawRow);
    return {
      ...row,
      components: row.components.map((button) => {
        const parsed = parseSelectionCustomId(buttonCustomId(button));
        if (!parsed) return { ...button };
        const selected = parsed.candidateId === selectedCandidateId;
        return {
          ...button,
          type: button.type ?? COMPONENT_TYPE_BUTTON,
          custom_id: buttonCustomId(button),
          style: selected ? BUTTON_STYLE_SUCCESS : BUTTON_STYLE_SECONDARY,
          label: selected
            ? `Selected as ${parsed.category} OF THE WEEK`
            : `Select as ${parsed.category} OF THE WEEK`,
          disabled: selected,
        };
      }),
    };
  });
}

function selectionReply(candidate) {
  const name = candidate.pov_player?.name ?? candidate.id;
  return `Selected ${name} as ${candidate.category} OF THE WEEK. Render queued.`;
}

// Per-category async mutex. Selections for the same category run strictly
// one after another (read state -> flip statuses -> enqueue is one critical
// section); different categories stay concurrent. In-process only — the bot
// is the single selection writer by design (see bot/README.md).
const selectionLocks = new Map();

export async function withCategoryLock(category, fn) {
  const previous = selectionLocks.get(category) ?? Promise.resolve();
  const current = previous.then(fn);
  const settled = current.then(() => {}, () => {});
  selectionLocks.set(category, settled);
  settled.then(() => {
    if (selectionLocks.get(category) === settled) selectionLocks.delete(category);
  });
  return current;
}

export async function applySelection({ category, candidateId, store, queue }) {
  return withCategoryLock(category, async () => {
    const candidates = await store.list({ category });
    const target = candidates.find((candidate) => candidate.id === candidateId);
    if (!target) throw new Error(`candidate ${candidateId} is not in ${category}`);

    const selected = { ...target, status: 'selected' };

    // Idempotent re-click: the candidate is already selected and already has
    // a pending render — converge without new transitions or queue entries.
    if (target.status === 'selected') {
      const pending = await queue.pending({ category });
      const existing = pending.find((entry) => entry.candidate_id === candidateId);
      if (existing) return { selected, previous: null, queueEntry: existing };
    }

    const previous = candidates.find(
      (candidate) => candidate.status === 'selected' && candidate.id !== candidateId,
    );
    if (previous) await store.updateStatus(previous.id, 'posted');
    if (target.status !== 'selected') await store.updateStatus(target.id, 'selected');

    const queueEntry = await queue.enqueue(selected);
    return { selected, previous, queueEntry };
  });
}

export async function handleSelectionInteraction(interaction, { store, queue, log = () => {} }) {
  if (interaction.isButton && !interaction.isButton()) return false;
  const parsed = parseSelectionCustomId(interaction.customId);
  if (!parsed) return false;

  // Acknowledge FIRST (review finding P1, PR #42): store/queue work shells
  // out to a Python subprocess and can blow Discord's 3-second interaction
  // window. deferUpdate() acks immediately; the component edit then lands
  // via editReply() inside the 15-minute token window (same pattern as
  // /clipsmith status in commands.mjs).
  let deferred = false;
  if (interaction.deferUpdate) {
    try {
      await interaction.deferUpdate();
      deferred = true;
    } catch (err) {
      log(`selection deferUpdate failed: ${err.message ?? err}`);
    }
  }

  try {
    const result = await applySelection({
      category: parsed.category,
      candidateId: parsed.candidateId,
      store,
      queue,
    });
    const payload = {
      components: markSelectedComponents(
        interaction.message?.components ?? [],
        result.selected.id,
      ),
    };
    if (deferred && interaction.editReply) await interaction.editReply(payload);
    else if (interaction.update) await interaction.update(payload);
    else if (interaction.reply) {
      await interaction.reply({ content: selectionReply(result.selected), ephemeral: true });
    }
  } catch (err) {
    log(`selection failed: ${err.message ?? err}`);
    const failure = {
      content: `Selection failed: ${err.message ?? err}`,
      ephemeral: true,
    };
    if (deferred && interaction.followUp) await interaction.followUp(failure);
    else if (interaction.reply) await interaction.reply(failure);
  }
  return true;
}

export function parseTextSelection(content) {
  const match = /^\s*select\s+(\d+)\s*$/i.exec(content ?? '');
  return match ? Number(match[1]) : null;
}

export async function handleTextSelectionMessage(message, {
  store, queue, fetchMessage, log = () => {},
}) {
  const index = parseTextSelection(message.content);
  if (index == null) return false;
  const refId = message.reference?.messageId;
  if (!refId) {
    await message.reply?.({ content: 'Reply to a digest category message with `select <n>`.' });
    return true;
  }

  const digestMessage = await fetchMessage(refId);
  const selections = selectionsFromMessage(digestMessage);
  if (selections.length === 0) {
    // Empty category / non-digest message: no buttons to map numbers onto
    // (review finding P2, PR #42 — was "Choose a number from 1 to 0").
    await message.reply?.({
      content: 'That message has no selectable candidates — reply under a digest '
        + 'category message that lists candidates.',
    });
    return true;
  }
  const picked = selections.find((selection) => selection.index === index);
  if (!picked) {
    await message.reply?.({
      content: `Choose a number from 1 to ${selections.length} for this category.`,
    });
    return true;
  }

  try {
    const result = await applySelection({
      category: picked.category,
      candidateId: picked.candidateId,
      store,
      queue,
    });
    await digestMessage.edit?.({
      components: markSelectedComponents(digestMessage.components ?? [], result.selected.id),
    });
    await message.reply?.({ content: selectionReply(result.selected) });
  } catch (err) {
    log(`text selection failed: ${err.message ?? err}`);
    await message.reply?.({ content: `Selection failed: ${err.message ?? err}` });
  }
  return true;
}
