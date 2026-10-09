// Mobster's MCP server in the Vercel AI SDK.
//
//   node index.mjs --list    list Mobster's tools (no key needed)
//   node index.mjs           let Claude call Mobster's read-only status tool once (needs ANTHROPIC_API_KEY)
//
// The model is given only `status`, so it can't touch a simulator or a phone.
import { execFileSync } from 'node:child_process';
import { createMCPClient } from '@ai-sdk/mcp';
import { Experimental_StdioMCPTransport as StdioMCPTransport } from '@ai-sdk/mcp/mcp-stdio';

function mobster() {
  if (process.env.MOBSTER_BIN) return process.env.MOBSTER_BIN;
  try {
    return execFileSync('/usr/bin/which', ['mobster'], { encoding: 'utf8' }).trim();
  } catch {
    console.error("mobster isn't on PATH. Install it (curl -fsSL https://mobster.dev/install.sh | sh) or set MOBSTER_BIN.");
    process.exit(1);
  }
}

const client = await createMCPClient({
  transport: new StdioMCPTransport({ command: mobster(), args: ['mcp', '--keyless'] }),
});
try {
  const tools = await client.tools();
  if (process.argv.includes('--list')) {
    console.log(Object.keys(tools).join(', '));
  } else {
    const { generateText, isStepCount } = await import('ai');
    const { anthropic } = await import('@ai-sdk/anthropic');
    const { text } = await generateText({
      model: anthropic('claude-sonnet-5-5'),
      tools: { status: tools.status },
      stopWhen: isStepCount(3),
      prompt: "Call Mobster's status tool once. Say which Mobster version runs and whether Smart is on.",
    });
    console.log(text);
  }
} finally {
  await client.close();
}
