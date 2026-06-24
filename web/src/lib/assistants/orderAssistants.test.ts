import { describe, it, expect } from "vitest";
import { orderAssistantsForUser } from "./orderAssistants";
import { Persona } from "@/app/admin/assistants/interfaces";
import { User } from "../types";

// Minimal fixtures — orderAssistantsForUser only reads `.id` off personas and
// `.preferences` off the user, so we cast trimmed shapes.
const p = (id: number): Persona => ({ id }) as unknown as Persona;
const userWith = (
  chosen: number[] | null,
  hidden?: number[] | null
): User =>
  ({
    preferences: { chosen_assistants: chosen, hidden_assistants: hidden },
  }) as unknown as User;

const ids = (list: Persona[]) => list.map((a) => a.id);

describe("orderAssistantsForUser (opt-out visibility)", () => {
  const all = [p(1), p(2), p(3), p(4)];

  it("shows every assistant in input order when there are no preferences", () => {
    expect(ids(orderAssistantsForUser(all, null))).toEqual([1, 2, 3, 4]);
    expect(ids(orderAssistantsForUser(all, userWith(null, null)))).toEqual([
      1, 2, 3, 4,
    ]);
  });

  it("hides only the assistants listed in hidden_assistants", () => {
    const user = userWith(null, [2, 4]);
    expect(ids(orderAssistantsForUser(all, user))).toEqual([1, 3]);
  });

  it("shows a brand-new assistant (not hidden, not chosen) by default", () => {
    // User curated [3,1] long ago; assistant 4 was created afterwards.
    const user = userWith([3, 1], []);
    const result = ids(orderAssistantsForUser(all, user));
    // chosen come first in chosen order, then the rest in input order.
    expect(result).toEqual([3, 1, 2, 4]);
    expect(result).toContain(4); // the key regression: new assistant appears
  });

  it("orders chosen ids first, remaining keep their incoming order", () => {
    const user = userWith([4, 2], []);
    expect(ids(orderAssistantsForUser(all, user))).toEqual([4, 2, 1, 3]);
  });

  it("hidden wins even if the id is also present in chosen_assistants", () => {
    const user = userWith([1, 2, 3], [2]);
    expect(ids(orderAssistantsForUser(all, user))).toEqual([1, 3, 4]);
  });

  it("returns the visible set when chosen_assistants is empty", () => {
    const user = userWith([], [1]);
    expect(ids(orderAssistantsForUser(all, user))).toEqual([2, 3, 4]);
  });

  it("does not mutate the input array", () => {
    const input = [p(1), p(2), p(3)];
    orderAssistantsForUser(input, userWith([3], []));
    expect(ids(input)).toEqual([1, 2, 3]);
  });
});
