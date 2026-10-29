export type Comment = { path: string; line: number; code: string; text: string };

/** Review comments as one message for the agent. The quoted code is cut by characters (code points): cutting by
 * UTF-16 units could leave half an emoji, which the server refuses as invalid text (break-it 9). */
export function feedbackMessage(comments: Comment[]): string {
  return ["Review feedback on your changes:", ...comments.map((c) =>
    `- ${c.path}:${c.line}${c.code ? ` \`${[...c.code.replace(/`/g, "'")].slice(0, 160).join("")}\`` : ""} — ${c.text.trim()}`)].join("\n");
}
