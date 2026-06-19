// Single source of truth for what an assistant is *called* in the chat UI.
// Admins can set an optional, friendlier `display_name`; the immutable `name`
// stays the identifier and is used everywhere else (admin tables, matching).
// Falls back to `name` when `display_name` is unset/blank.
export function assistantDisplayName(persona: {
  display_name?: string | null;
  name: string;
}): string {
  const dn = persona.display_name?.trim();
  return dn ? dn : persona.name;
}
