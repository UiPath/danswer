// PATCH the user's full `chosen_assistants` array. This single endpoint
// drives every preference mutation below — visibility, ordering, default
// — because the backend treats the array as both "which assistants are
// visible in the picker" (membership) AND "in what order" (positions),
// with position 0 = default.
async function updateUserAssistantList(
  chosenAssistants: number[]
): Promise<boolean> {
  const response = await fetch("/api/user/assistant-list", {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ chosen_assistants: chosenAssistants }),
  });

  return response.ok;
}

export async function removeAssistantFromList(
  assistantId: number,
  chosenAssistants: number[]
): Promise<boolean> {
  const updatedAssistants = chosenAssistants.filter((id) => id !== assistantId);
  return updateUserAssistantList(updatedAssistants);
}

export async function addAssistantToList(
  assistantId: number,
  chosenAssistants: number[]
): Promise<boolean> {
  if (!chosenAssistants.includes(assistantId)) {
    const updatedAssistants = [...chosenAssistants, assistantId];
    return updateUserAssistantList(updatedAssistants);
  }
  return false;
}

export async function moveAssistantUp(
  assistantId: number,
  chosenAssistants: number[]
): Promise<boolean> {
  const index = chosenAssistants.indexOf(assistantId);
  if (index > 0) {
    [chosenAssistants[index - 1], chosenAssistants[index]] = [
      chosenAssistants[index],
      chosenAssistants[index - 1],
    ];
    return updateUserAssistantList(chosenAssistants);
  }
  return false;
}

export async function moveAssistantDown(
  assistantId: number,
  chosenAssistants: number[]
): Promise<boolean> {
  const index = chosenAssistants.indexOf(assistantId);
  if (index < chosenAssistants.length - 1) {
    [chosenAssistants[index + 1], chosenAssistants[index]] = [
      chosenAssistants[index],
      chosenAssistants[index + 1],
    ];
    return updateUserAssistantList(chosenAssistants);
  }
  return false;
}

// ---------------------------------------------------------------------------
// Hidden-assistants (opt-out visibility). `hidden_assistants` is the source of
// truth for what's shown in the picker; `chosen_assistants` (above) only orders
// what's visible. Anything NOT hidden is visible by default — so newly created
// assistants appear for everyone automatically.
// ---------------------------------------------------------------------------

/** PATCH the user's full `hidden_assistants` array. */
export async function setHiddenAssistants(
  hiddenAssistants: number[]
): Promise<boolean> {
  const response = await fetch("/api/user/hidden-assistants", {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ hidden_assistants: hiddenAssistants }),
  });

  return response.ok;
}

/** Hide a single assistant (add to the hidden list; idempotent). */
export async function hideAssistant(
  assistantId: number,
  hiddenAssistants: number[]
): Promise<boolean> {
  if (hiddenAssistants.includes(assistantId)) {
    return true;
  }
  return setHiddenAssistants([...hiddenAssistants, assistantId]);
}

/** Show a single assistant (remove from the hidden list; idempotent). */
export async function unhideAssistant(
  assistantId: number,
  hiddenAssistants: number[]
): Promise<boolean> {
  return setHiddenAssistants(
    hiddenAssistants.filter((id) => id !== assistantId)
  );
}

// ---------------------------------------------------------------------------
// Used by the new Manage Assistants UX
// ---------------------------------------------------------------------------

/** Replace the user's full chosen_assistants list (drag-reorder, bulk ops). */
export async function reorderAssistantList(
  newOrder: number[]
): Promise<boolean> {
  return updateUserAssistantList(newOrder);
}

/**
 * Move `assistantId` to position 0 so it becomes the user's default. If the
 * id isn't in the list it's prepended (i.e. set-as-default also unhides it).
 */
export async function setDefaultAssistant(
  assistantId: number,
  chosenAssistants: number[]
): Promise<boolean> {
  const withoutTarget = chosenAssistants.filter((id) => id !== assistantId);
  return updateUserAssistantList([assistantId, ...withoutTarget]);
}

/** Bulk: hide a set of assistant ids (remove them from chosen_assistants). */
export async function bulkRemoveFromList(
  assistantIds: number[],
  chosenAssistants: number[]
): Promise<boolean> {
  const toRemove = new Set(assistantIds);
  return updateUserAssistantList(
    chosenAssistants.filter((id) => !toRemove.has(id))
  );
}

/** Bulk: add a set of assistant ids to the visible list (appended at the end). */
export async function bulkAddToList(
  assistantIds: number[],
  chosenAssistants: number[]
): Promise<boolean> {
  const existing = new Set(chosenAssistants);
  const toAppend = assistantIds.filter((id) => !existing.has(id));
  if (toAppend.length === 0) {
    // Nothing to do, but report success so the UI can clear its selection.
    return true;
  }
  return updateUserAssistantList([...chosenAssistants, ...toAppend]);
}
