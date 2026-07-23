// Slash commands (issue #16 step 3): /clipsmith status — read-only
// candidate-store counts grouped by status.
//
// Command definitions are plain API JSON (no discord.js import) so the
// handler is unit-testable with mocked interaction objects.
import { STATUSES } from './store.mjs';

// Discord API: type 1 = SUB_COMMAND.
export const commandDefinitions = [
  {
    name: 'clipsmith',
    description: 'clipsmith pipeline utilities',
    options: [
      {
        type: 1,
        name: 'status',
        description: 'Candidate-store counts by status',
      },
    ],
  },
];

/** Monospace table of counts, one row per status, plus a total. */
export function formatStatusReply(counts) {
  const width = Math.max(...STATUSES.map((s) => s.length));
  const lines = STATUSES.map((s) => `${s.padEnd(width)}  ${counts[s] ?? 0}`);
  const total = STATUSES.reduce((sum, s) => sum + (counts[s] ?? 0), 0);
  lines.push(`${'total'.padEnd(width)}  ${total}`);
  return '**Candidate store — counts by status**\n```\n' + lines.join('\n') + '\n```';
}

/**
 * Route an interaction. Returns true when it was ours and handled.
 * `interaction` only needs isChatInputCommand()/commandName/options/
 * reply/deferReply/editReply — tests pass plain objects.
 */
export async function handleInteraction(interaction, { store, log = () => {} }) {
  if (!interaction.isChatInputCommand?.()) return false;
  if (interaction.commandName !== 'clipsmith') return false;
  const sub = interaction.options.getSubcommand();
  if (sub !== 'status') {
    // Partial deployments (#17/#18) can register subcommands this build does
    // not handle yet; answer instead of ghosting ("interaction failed").
    log(`unknown /clipsmith subcommand: ${sub}`);
    await interaction.reply({
      content: `Unknown subcommand \`${sub}\` — this bot build does not support it yet.`,
      ephemeral: true,
    });
    return true;
  }
  // countsByStatus() shells out to a Python subprocess; acknowledge within
  // Discord's 3-second window first, then edit the deferred reply.
  await interaction.deferReply();
  try {
    const counts = await store.countsByStatus();
    await interaction.editReply({ content: formatStatusReply(counts) });
  } catch (err) {
    log('status command failed:', err.message ?? err);
    await interaction.editReply({
      content: 'Candidate store unreachable — check the bot host.',
    });
  }
  return true;
}
