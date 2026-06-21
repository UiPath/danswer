import { Persona } from "@/app/admin/assistants/interfaces";
import { User } from "../types";

/**
 * Compute the assistants a user should see in the chat picker, in order.
 *
 * Opt-OUT model: every accessible assistant is visible UNLESS the user has
 * explicitly hidden it (`hidden_assistants`). This is why a newly created
 * (e.g. admin) assistant shows up for everyone automatically — it's in nobody's
 * hidden list.
 *
 * Ordering: assistants listed in `chosen_assistants` come first, in that order
 * (position 0 is the user's default); any remaining visible assistant keeps its
 * incoming order (the backend already sorts by display_priority etc.). So a
 * brand-new assistant appears after the user's pinned/ordered ones rather than
 * jumping to the top.
 */
export function orderAssistantsForUser(
  assistants: Persona[],
  user: User | null
): Persona[] {
  const hidden = new Set(user?.preferences?.hidden_assistants ?? []);
  const visible = assistants.filter((assistant) => !hidden.has(assistant.id));

  const chosen = user?.preferences?.chosen_assistants;
  if (!chosen || chosen.length === 0) {
    return visible;
  }

  const orderMap = new Map<number, number>(
    chosen.map((id: number, index: number) => [id, index])
  );

  // Stable sort: chosen ids by their chosen index first, then the rest in their
  // original relative order (decorate-sort-undecorate to keep stability).
  return visible
    .map((assistant, index) => ({ assistant, index }))
    .sort((a, b) => {
      const orderA = orderMap.get(a.assistant.id);
      const orderB = orderMap.get(b.assistant.id);
      if (orderA !== undefined && orderB !== undefined) return orderA - orderB;
      if (orderA !== undefined) return -1;
      if (orderB !== undefined) return 1;
      return a.index - b.index;
    })
    .map(({ assistant }) => assistant);
}
