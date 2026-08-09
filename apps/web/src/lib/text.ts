/**
 * Strip internal protocol markers the backend embeds in message bodies
 * (HTML comments such as `<!-- search:done -->`). Display-only: the stored
 * message data is never touched. Needed because react-markdown without
 * rehype-raw renders HTML comments as literal text.
 */
export function stripInternalMarkers(text: string): string {
  return text.replace(/<!--[\s\S]*?-->/g, "");
}
