import type { ChatReply, HelperTool } from './api'

/** WebMCP is a draft: `document.modelContext` only exists behind an origin trial, so all of it lives in this file and
 *  the app works the same without it. Another agent in this browser gets the chat's tools: nothing that spends or deletes. */
type ModelContext = {
  registerTool: (
    tool: {
      name: string
      description: string
      inputSchema: unknown
      execute: (args: Record<string, unknown>) => Promise<{ content: { type: 'text'; text: string }[] }>
    },
    options?: { signal?: AbortSignal },
  ) => void
}

export function registerWebMcp(tools: HelperTool[], run: (name: string, args: Record<string, unknown>) => Promise<ChatReply>) {
  const context = (document as unknown as { modelContext?: ModelContext }).modelContext
  if (!context?.registerTool) return () => {}
  const stop = new AbortController()
  for (const t of tools) {
    try {
      context.registerTool(
        {
          name: `seam_${t.name}`,
          description: t.description,
          inputSchema: {
            type: 'object',
            properties: Object.fromEntries(Object.entries(t.args).map(([k, type]) => [k, { type }])),
            required: Object.keys(t.args),
          },
          execute: async (args) => ({ content: [{ type: 'text', text: (await run(t.name, args)).say }] }),
        },
        { signal: stop.signal },
      )
    } catch {
      // the API moved or refused this tool: the page does not depend on it
    }
  }
  return () => stop.abort()
}
